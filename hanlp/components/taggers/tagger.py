# -*- coding:utf-8 -*-
# Author: hankcs
# Date: 2020-08-11 12:19
import logging
import warnings
from abc import ABC, abstractmethod
from typing import List, TextIO, Any, Union, Dict, Tuple, Sequence

import torch
from torch import optim, nn
from torch.utils.data import DataLoader

from hanlp_common.constant import IDX
from hanlp.common.structure import History
from hanlp.components.distillation.distillable_component import DistillableComponent
from hanlp.components.taggers.util import guess_tagging_scheme
from hanlp.layers.crf.crf import CRF
from hanlp.metrics.accuracy import CategoricalAccuracy
from hanlp.utils.time_util import CountdownTimer
from hanlp_common.util import reorder
from hanlp_trie import DictInterface, TrieDict
from hanlp_trie.dictionary import TupleTrieDict


class Tagger(DistillableComponent, ABC):
    def build_optimizer(self, optimizer, lr, **kwargs):
        if optimizer == 'adam':
            return optim.Adam(params=self.model.parameters(), lr=lr)
        elif optimizer == 'sgd':
            return torch.optim.SGD(self.model.parameters(), lr=lr)

    def build_criterion(self, model=None, reduction='mean', **kwargs):
        if self.config.get('crf', False):
            if not model:
                model = self.model
            if isinstance(model, nn.DataParallel):
                raise ValueError('DataParallel not supported when CRF is used')
                return self.model_from_config.module.crf
            return model.crf
        else:
            return nn.CrossEntropyLoss(reduction=reduction)

    def build_metric(self, **kwargs):
        return CategoricalAccuracy()

    @abstractmethod
    def feed_batch(self, batch):
        pass

    def compute_loss(self, criterion, out, y, mask):
        if self.config.get('crf', False):
            criterion: CRF = criterion
            loss = -criterion.forward(out, y, mask)
        else:
            loss = criterion(out[mask], y[mask])
        return loss

    def decode_output(self, logits, mask, batch, model=None):
        if self.config.get('crf', False):
            if model is None:
                model = self.model
            crf: CRF = model.crf
            outputs = crf.decode(logits, mask)
            return [y[0] for y in outputs]
        else:
            return logits.argmax(-1)

    def execute_training_loop(self, trn: DataLoader, dev: DataLoader, epochs, criterion, optimizer, metric, save_dir,
                              logger: logging.Logger, devices, ratio_width=None, patience=5, teacher=None,
                              kd_criterion=None,
                              **kwargs):
        best_epoch, best_metric = 0, -1
        timer = CountdownTimer(epochs)
        history = History()
        for epoch in range(1, epochs + 1):
            logger.info(f"[yellow]Epoch {epoch} / {epochs}:[/yellow]")
            self.fit_dataloader(trn, criterion, optimizer, metric, logger, history=history, ratio_width=ratio_width,
                                **self.config)
            loss, dev_metric = self.evaluate_dataloader(dev, criterion, logger=logger, ratio_width=ratio_width)
            timer.update()
            report = f"{timer.elapsed_human} / {timer.total_time_human} ETA: {timer.eta_human}"
            if dev_metric > best_metric:
                best_epoch, best_metric = epoch, dev_metric
                self.save_weights(save_dir)
                report += ' [red](saved)[/red]'
            else:
                report += f' ({epoch - best_epoch})'
                if epoch - best_epoch >= patience:
                    report += ' early stop'
            logger.info(report)
            if epoch - best_epoch >= patience:
                break
        if not best_epoch:
            self.save_weights(save_dir)
        elif best_epoch != epoch:
            self.load_weights(save_dir)
        logger.info(f"Max score of dev is {best_metric} at epoch {best_epoch}")
        logger.info(f"Average time of each epoch is {timer.elapsed_average_human}")
        logger.info(f"{timer.elapsed_human} elapsed")
        return best_metric

    def id_to_tags(self, ids: torch.LongTensor, lens: List[int]):
        batch = []
        vocab = self.vocabs['tag'].idx_to_token
        for b, l in zip(ids, lens):
            batch.append([])
            for i in b[:l]:
                batch[-1].append(vocab[i])
        return batch

    def update_metrics(self, metric, logits, y, mask, batch=None, prediction=None):
        metric(logits, y, mask)

    @torch.no_grad()
    def evaluate_dataloader(self, data, criterion, logger=None, ratio_width=None, metric=None, output=None, **kwargs):
        self.model.eval()
        if isinstance(output, str):
            output = open(output, 'w')

        loss = 0
        if not metric:
            metric = self.build_metric()
        else:
            metric.reset()
        timer = CountdownTimer(len(data))
        for idx, batch in enumerate(data):
            logits, mask = self.feed_batch(batch)
            y = batch['tag_id']
            loss += self.compute_loss(criterion, logits, y, mask).item()
            prediction = self.decode_output(logits, mask, batch)
            self.update_metrics(metric, logits, y, mask, batch, prediction)
            if output:
                self.write_prediction(prediction, batch, output)
            timer.log(f'loss: {loss / (idx + 1):.4f} {metric}', ratio_percentage=False, logger=logger,
                      ratio_width=ratio_width)
        loss /= len(data)
        if output:
            output.close()
        return float(loss), metric

    def write_prediction(self, prediction, batch, output: TextIO):
        for tokens, ps, gs in zip(batch[self.config.token_key], prediction, batch['tag']):
            output.write('\n'.join('\t'.join([t, p, g]) for t, p, g in zip(tokens, ps, gs)))
            output.write('\n')

    def predict(self, tokens: Any, batch_size: int = None, **kwargs):
        if not tokens:
            return []
        flat = self.input_is_flat(tokens)
        if flat:
            tokens = [tokens]
        outputs = self.predict_data(tokens, batch_size, **kwargs)
        if flat:
            return outputs[0]
        return outputs

    def input_is_flat(self, tokens):
        return isinstance(tokens, list) and isinstance(tokens[0], str)

    def predict_data(self, data, batch_size, sampler_builder=None, **kwargs):
        samples = self.build_samples(data, **kwargs)
        if not batch_size:
            batch_size = self.config.get('batch_size', 32)
        dataloader = self.build_dataloader(samples, batch_size, False, self.device, sampler_builder=sampler_builder,
                                           **kwargs)
        outputs = []
        orders = []
        vocab = self.vocabs['tag'].idx_to_token
        for batch in dataloader:
            out, mask = self.feed_batch(batch)
            pred = self.decode_output(out, mask, batch)
            outputs.extend(self.prediction_to_human(pred, vocab, batch))
            orders.extend(batch[IDX])
        outputs = reorder(outputs, orders)
        return outputs

    def build_samples(self, data: List[str], **kwargs):
        return [{self.config.token_key: sent} for sent in data]

    def prediction_to_human(self, pred_ids, vocab: List[str], batch):
        if isinstance(pred_ids, torch.Tensor):
            pred_ids = pred_ids.tolist()
        sents = batch[self.config.token_key]
        dict_tags: DictInterface = self.dict_tags
        for each, sent in zip(pred_ids, sents):
            tags = [vocab[id] for id in each[:len(sent)]]
            if dict_tags:
                for begin, end, label in dict_tags.tokenize(sent):
                    tags[begin:end] = label
            yield tags

    @property
    def tagging_scheme(self):
        tagging_scheme = self.config.tagging_scheme
        if not tagging_scheme:
            self.config.tagging_scheme = tagging_scheme = guess_tagging_scheme(self.vocabs.tag.idx_to_token)
            if tagging_scheme == 'BIO':
                warnings.warn(f'The tag scheme for {self.vocabs.tag.idx_to_token} might be IOB1 or IOB2 '
                              f'but we are using IOB2 by default. Please set tagging_scheme="IOB1" or tagging_scheme="BIO" '
                              f'to get rid of this warning.')
        return tagging_scheme

    @property
    def dict_tags(self) -> DictInterface:
        r""" A custom dictionary to override predicted tags by performing longest-prefix-matching.

        Examples:
            >>> pos.dict_tags = {'HanLP': 'state-of-the-art-tool'} # Force 'HanLP' to be 'state-of-the-art-tool'
            >>> tagger("HanLP为生产环境带来次世代最先进的多语种NLP技术。")
                # HanLP/state-of-the-art-tool 为/P 生产/NN 环境/NN 带来/VV 次世代/NN 最/AD 先进/VA 的/DEC 多语种/NN NLP/NR 技术/NN 。/PU
            >>> pos.dict_tags = {('的', '希望'): ('补语成分', '名词'), '希望': '动词'} # Conditional matching
            >>> tagger("我的希望是希望张晚霞的背影被晚霞映红。")
                # 我/PN 的/补语成分 希望/名词 是/VC 希望/动词 张晚霞/NR 的/DEG 背影/NN 被/LB 晚霞/NN 映红/VV 。/PU
        """
        return self.config.get('dict_tags', None)

    @dict_tags.setter
    def dict_tags(self,
                  dictionary: Union[DictInterface, Union[Dict[Union[str, Sequence[str]], Union[str, Sequence[str]]]]]):
        if dictionary is not None and not isinstance(dictionary, DictInterface):
            _d = dict()
            for k, v in dictionary.items():
                if isinstance(k, str):
                    k = (k,)
                if isinstance(v, str):
                    v = (v,) * len(k)
                _d[k] = v
            dictionary = TupleTrieDict(_d)
        self.config.dict_tags = dictionary
