"""
An extensible implementation of the Causal Bert model from 
"Adapting Text Embeddings for Causal Inference" 
    (https://arxiv.org/abs/1905.12741)
"""
import random
import logging
import argparse
import pandas as pd
import matplotlib.pyplot as plt
from collections import defaultdict

from torch.utils.data import TensorDataset, DataLoader, RandomSampler, SequentialSampler

from transformers import AdamW
from transformers import get_linear_schedule_with_warmup

from transformers import DistilBertTokenizer
from transformers import DistilBertModel, DistilBertPreTrainedModel

from torch.nn import CrossEntropyLoss

import torch
import torch.nn as nn
import numpy as np
from scipy.special import logit
from sklearn.linear_model import LogisticRegression

from tqdm import tqdm
import math

CUDA = (torch.cuda.device_count() > 0)
MASK_IDX = 103
logger = logging.getLogger()

random.seed(0)
np.random.seed(0)
torch.manual_seed(0)


def platt_scale(outcome, probs):
    logits = logit(probs)
    logits = logits.reshape(-1, 1)
    log_reg = LogisticRegression(penalty='none', warm_start=True, solver='lbfgs')
    log_reg.fit(logits, outcome)
    return log_reg.predict_proba(logits)


def gelu(x):
    return 0.5 * x * (1.0 + torch.erf(x / math.sqrt(2.0)))


def make_bow_vector(ids, vocab_size, use_counts=False):
    """ Make a sparse BOW vector from a tensor of dense ids.
    Args:
        ids: torch.LongTensor [batch, features]. Dense tensor of ids.
        vocab_size: vocab size for this tensor.
        use_counts: if true, the outgoing BOW vector will contain
            feature counts. If false, will contain binary indicators.
    Returns:
        The sparse bag-of-words representation of ids.
    """
    vec = torch.zeros(ids.shape[0], vocab_size)
    ones = torch.ones_like(ids, dtype=torch.float)
    if CUDA:
        vec = vec.cuda()
        ones = ones.cuda()
        ids = ids.cuda()

    vec.scatter_add_(1, ids, ones)
    vec[:, 1] = 0.0  # zero out pad
    if not use_counts:
        vec = (vec != 0).float()
    return vec


class CausalBert(DistilBertPreTrainedModel):
    """The model itself."""
    def __init__(self, config):
        super().__init__(config)

        self.num_labels = config.num_labels
        self.vocab_size = config.vocab_size

        self.distilbert = DistilBertModel(config)
        # self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.vocab_transform = nn.Linear(config.dim, config.dim)
        self.vocab_layer_norm = nn.LayerNorm(config.dim, eps=1e-12)
        self.vocab_projector = nn.Linear(config.dim, config.vocab_size)

        self.Q_cls = nn.ModuleDict()

        for T in range(2):
            # ModuleDict keys have to be strings..
            self.Q_cls['%d' % T] = nn.Sequential(
                nn.Linear(config.hidden_size + self.num_labels, 200),
                nn.ReLU(),
                nn.Linear(200, self.num_labels))

        self.g_cls = nn.Linear(config.hidden_size + self.num_labels, 
            self.config.num_labels)

        self.init_weights()

    def forward(self, W_ids, W_len, W_mask, C, T, Y=None, use_mlm=True):
        if use_mlm:
            W_len = W_len.unsqueeze(1) - 2 # -2 because of the +1 below
            mask_class = torch.cuda.FloatTensor if CUDA else torch.FloatTensor
            mask = (mask_class(W_len.shape).uniform_() * W_len.float()).long() + 1 # + 1 to avoid CLS
            target_words = torch.gather(W_ids, 1, mask)
            mlm_labels = torch.ones(W_ids.shape).long() * -100
            if CUDA:
                mlm_labels = mlm_labels.cuda()
            mlm_labels.scatter_(1, mask, target_words)
            W_ids.scatter_(1, mask, MASK_IDX)

        outputs = self.distilbert(W_ids, attention_mask=W_mask)
        seq_output = outputs[0]
        pooled_output = seq_output[:, 0]
        # seq_output, pooled_output = outputs[:2]
        # pooled_output = self.dropout(pooled_output)

        if use_mlm:
            prediction_logits = self.vocab_transform(seq_output)  # (bs, seq_length, dim)
            prediction_logits = gelu(prediction_logits)  # (bs, seq_length, dim)
            prediction_logits = self.vocab_layer_norm(prediction_logits)  # (bs, seq_length, dim)
            prediction_logits = self.vocab_projector(prediction_logits)  # (bs, seq_length, vocab_size)
            mlm_loss = CrossEntropyLoss()(
                prediction_logits.view(-1, self.vocab_size), mlm_labels.view(-1))
        else:
            mlm_loss = 0.0

        C_bow = make_bow_vector(C.unsqueeze(1), self.num_labels)
        inputs = torch.cat((pooled_output, C_bow), 1)

        # g logits
        g = self.g_cls(inputs)
        if Y is not None:  # TODO train/test mode, this is a lil hacky
            g_loss = CrossEntropyLoss()(g.view(-1, self.num_labels), T.view(-1))
        else:
            g_loss = 0.0

        # conditional expected outcome logits: 
        # run each example through its corresponding T matrix
        # TODO this would be cleaner with sigmoid and BCELoss, but less general 
        #   (and I couldn't get it to work as well)
        Q_logits_T0 = self.Q_cls['0'](inputs)
        Q_logits_T1 = self.Q_cls['1'](inputs)

        if Y is not None:
            T0_indices = (T == 0).nonzero().squeeze()
            Y_T1_labels = Y.clone().scatter(0, T0_indices, -100)

            T1_indices = (T == 1).nonzero().squeeze()
            Y_T0_labels = Y.clone().scatter(0, T1_indices, -100)

            Q_loss_T1 = CrossEntropyLoss()(
                Q_logits_T1.view(-1, self.num_labels), Y_T1_labels)
            Q_loss_T0 = CrossEntropyLoss()(
                Q_logits_T0.view(-1, self.num_labels), Y_T0_labels)

            Q_loss = Q_loss_T0 + Q_loss_T1
        else:
            Q_loss = 0.0

        sm = nn.Softmax(dim=1)
        Q0 = sm(Q_logits_T0)[:, 1]
        Q1 = sm(Q_logits_T1)[:, 1]
        g = sm(g)[:, 1]

        return g, Q0, Q1, g_loss, Q_loss, mlm_loss


class CausalBertWrapper:
    """Model wrapper in charge of training and inference."""

    def __init__(self, g_weight=1.0, Q_weight=0.1, mlm_weight=1.0, batch_size=32):
        self.model = CausalBert.from_pretrained("distilbert-base-uncased",
                                                num_labels=2,
                                                output_attentions=False,
                                                output_hidden_states=False)
        if CUDA:
            self.model = self.model.cuda()

        self.loss_weights = {
            'g': g_weight,
            'Q': Q_weight,
            'mlm': mlm_weight
        }
        self.batch_size = batch_size

    def calculate_total_loss(self, g_loss, Q_loss, mlm_loss):
        loss \
            = self.loss_weights['g'] * g_loss + self.loss_weights['Q'] * Q_loss + self.loss_weights['mlm'] * mlm_loss
        return loss

    def train(self, train, dev, learning_rate=2e-5, epochs=3):
        dataloader = self.build_dataloader(train['text'], train['C'], train['T'], train['Y'])
        self.model.train()
        optimizer = AdamW(self.model.parameters(), lr=learning_rate, eps=1e-8)
        total_steps = len(dataloader) * epochs
        warmup_steps = total_steps * 0.1
        scheduler = get_linear_schedule_with_warmup(optimizer,
                                                    num_warmup_steps=warmup_steps,
                                                    num_training_steps=total_steps)
        training_losses = {'epoch': [], 'total': [], 'g': [], 'Q': [], 'mlm': []}
        dev_losses = {'epoch': [], 'total': [], 'g': [], 'Q': [], 'mlm': []}

        for epoch in range(epochs):
            total_losses = []
            g_losses = []
            Q_losses = []
            mlm_losses = []
            self.model.train()

            for step, batch in tqdm(enumerate(dataloader), total=len(dataloader)):
                if CUDA:
                    batch = (x.cuda() for x in batch)
                W_ids, W_len, W_mask, C, T, Y = batch

                self.model.zero_grad()
                g, Q0, Q1, g_loss, Q_loss, mlm_loss = self.model(W_ids, W_len, W_mask, C, T, Y)
                loss = self.calculate_total_loss(g_loss, Q_loss, mlm_loss)

                loss.backward()
                optimizer.step()
                scheduler.step()

                total_losses.append(loss.detach().cpu().item())
                g_losses.append(g_loss.detach().cpu().item())
                Q_losses.append(Q_loss.detach().cpu().item())
                mlm_losses.append(mlm_loss.detach().cpu().item())

            training_total_loss = np.mean(total_losses)
            training_g_loss = np.mean(g_losses)
            training_Q_loss = np.mean(Q_losses)
            training_mlm_loss = np.mean(mlm_losses)

            logger.info("Epoch {} train total loss: {}".format(epoch, training_total_loss))
            logger.info("Epoch {} train propensity loss: {}".format(epoch, training_g_loss))
            logger.info("Epoch {} train conditional outcome loss: {}".format(epoch, training_Q_loss))
            logger.info("Epoch {} train masked language model loss: {}".format(epoch, training_mlm_loss))

            training_losses['epoch'].append(epoch)
            training_losses['total'].append(training_total_loss)
            training_losses['g'].append(training_g_loss)
            training_losses['mlm'].append(training_mlm_loss)
            training_losses['Q'].append(training_Q_loss)

            dev_total_loss, dev_g_loss, dev_Q_loss, dev_mlm_loss = self.evaluate_losses(dev)
            logger.info("Epoch {} dev total loss: {}".format(epoch, dev_total_loss))
            logger.info("Epoch {} dev propensity loss: {}".format(epoch, dev_g_loss))
            logger.info("Epoch {} dev conditional outcome loss: {}".format(epoch, dev_Q_loss))
            logger.info("Epoch {} dev masked language model loss: {}".format(epoch, dev_mlm_loss))

            dev_losses['epoch'].append(epoch)
            dev_losses['total'].append(dev_total_loss)
            dev_losses['g'].append(dev_g_loss)
            dev_losses['Q'].append(dev_Q_loss)
            dev_losses['mlm'].append(dev_mlm_loss)

        training_losses = pd.DataFrame.from_dict(training_losses)
        dev_losses = pd.DataFrame.from_dict(dev_losses)

        return training_losses, dev_losses

    def evaluate_losses(self, dev):
        self.model.eval()
        dataloader = self.build_dataloader(dev['text'], dev['C'], dev['T'], dev['Y'], sampler='sequential')

        with torch.no_grad():
            total_losses = []
            g_losses = []
            Q_losses = []
            mlm_losses = []
            for i, batch in tqdm(enumerate(dataloader), total=len(dataloader)):
                if CUDA:
                    batch = (x.cuda() for x in batch)
                W_ids, W_len, W_mask, C, T, Y = batch
                _, _, _, g_loss, Q_loss, mlm_loss = self.model(W_ids, W_len, W_mask, C, T, Y, use_mlm=False)
                total_loss = self.calculate_total_loss(g_loss, Q_loss, mlm_loss)
                total_losses.append(total_loss)
                g_losses.append(g_loss)
                Q_losses.append(Q_loss)
                mlm_losses.append(mlm_loss)
            total_loss = np.mean(total_losses)
            g_loss = np.mean(g_losses)
            Q_loss = np.mean(Q_losses)
            mlm_loss = np.mean(mlm_losses)
        return total_loss, g_loss, Q_loss, mlm_loss

    def inference(self, texts, confounds, outcome=None):
        self.model.eval()
        dataloader = self.build_dataloader(texts, confounds, outcomes=outcome, sampler='sequential')
        Q0s = []
        Q1s = []
        Ys = []
        gs = []
        for i, batch in tqdm(enumerate(dataloader), total=len(dataloader)):
            if CUDA: 
                batch = (x.cuda() for x in batch)
            W_ids, W_len, W_mask, C, T, Y = batch
            g, Q0, Q1, _, _, _ = self.model(W_ids, W_len, W_mask, C, T, use_mlm=False)
            Q0s += Q0.detach().cpu().numpy().tolist()
            Q1s += Q1.detach().cpu().numpy().tolist()
            Ys += Y.detach().cpu().numpy().tolist()
            gs += g.detach().cpu().numpy().tolist()
        probs = np.array(list(zip(Q0s, Q1s)))
        preds = np.argmax(probs, axis=1)
        gs = torch.tensor(gs, dtype=torch.float64)

        return probs, preds, Ys, gs

    def ATE(self, C, W, Y=None, platt_scaling=False):
        Q_probs, _, Ys, _ = self.inference(W, C, outcome=Y)
        if platt_scaling and Y is not None:
            Q0 = platt_scale(Ys, Q_probs[:, 0])[:, 0]
            Q1 = platt_scale(Ys, Q_probs[:, 1])[:, 1]
        else:
            Q0 = Q_probs[:, 0]
            Q1 = Q_probs[:, 1]

        return np.mean(Q0 - Q1)

    def ATT(self, C, W, T=None, Y=None, platt_scaling=False):
        Q_probs, _, Ys, gs = self.inference(W, C, outcome=Y)
        if platt_scaling and Y is not None:
            Q0 = platt_scale(Ys, Q_probs[:, 0])[:, 0].squeeze()
            Q1 = platt_scale(Ys, Q_probs[:, 1])[:, 1].squeeze()
        else:
            Q0 = Q_probs[:, 0].squeeze()
            Q1 = Q_probs[:, 1].squeeze()
        if T is None:
            T = torch.round(gs).type(torch.int)
        T1_indices = (T == 1).nonzero().squeeze()

        return np.mean(Q0[T1_indices] - Q1[T1_indices])

    def build_dataloader(self, texts, confounds, treatments=None, outcomes=None, tokenizer=None, sampler='random'):
        def collate_CandT(data):
            # sort by (C, T), so you can get boundaries later
            # (do this here on cpu for speed)
            data.sort(key=lambda x: (x[1], x[2]))
            return data
        # fill with dummy values
        if treatments is None:
            treatments = [-1 for _ in range(len(confounds))]
        if outcomes is None:
            outcomes = [-1 for _ in range(len(treatments))]

        if tokenizer is None:
            tokenizer = DistilBertTokenizer.from_pretrained(
                'distilbert-base-uncased', do_lower_case=True)

        out = defaultdict(list)
        for i, (W, C, T, Y) in enumerate(zip(texts, confounds, treatments, outcomes)):
            encoded_sent = tokenizer.encode_plus(W, add_special_tokens=True,
                max_length=128,
                truncation=True,
                pad_to_max_length=True)

            out['W_ids'].append(encoded_sent['input_ids'])
            out['W_mask'].append(encoded_sent['attention_mask'])
            out['W_len'].append(sum(encoded_sent['attention_mask']))
            out['Y'].append(Y)
            out['T'].append(T)
            out['C'].append(C)

        data = (torch.tensor(out[x]) for x in ['W_ids', 'W_len', 'W_mask', 'C', 'T', 'Y'])
        data = TensorDataset(*data)
        sampler = RandomSampler(data) if sampler == 'random' else SequentialSampler(data)
        dataloader = DataLoader(data, sampler=sampler, batch_size=self.batch_size)

        return dataloader


def main():
    parser = argparse.ArgumentParser(description='Sentiment Causal BERT')
    parser.add_argument('data', metavar='DATA', type=str, help='input data')
    parser.add_argument('--epochs', type=int, default=1, help='number of epochs to train')
    parser.add_argument('--format', choices=['json', 'csv'], help='file format of data')
    parser.add_argument('--outcome', type=str, default='Y', help="name of outcome column in data")
    parser.add_argument('--treatment', type=str, default='T', help="name of treatment column in data")
    parser.add_argument('--sentiment', action="store_true", help="flag indicating that treatment is sentiment")
    parser.add_argument('--confounder', type=str, help='name of out-of-text confounder column')
    parser.add_argument('--cutoff', type=float, default=0, help="Cut off for sentiment")
    parser.add_argument('--text', type=str, default='text', help="name of text column in data")
    parser.add_argument('--experiment', type=str, default='experiment', help="name of experiment")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s (%(relativeCreated)d ms) -> %(levelname)s: %(message)s',
                        datefmt='%I:%M:%S %p')

    logging.info("Reading data from {}".format(args.data))
    df = pd.DataFrame()
    if args.format == 'json':
        df = pd.read_json(args.data).T
    elif args.format == 'csv':
        df = pd.read_csv(args.data)

    logging.info("Preprocessing data...")

    if args.sentiment:
        logging.info("Using sentiment as treatment")
        logging.info("Positive sentiment set to be > {}".format(args.cutoff))
        df.loc[:, 'T'] = (df[args.treatment] > args.cutoff)
    else:
        logging.info("Not using sentiment as treatment")
        df.loc[:, 'T'] = df[args.treatment]
    df.loc[:, 'T'] = df['T'].astype(int)

    df.loc[:, 'Y'] = df[args.outcome].astype(int)

    if args.confounder is not None:
        df.loc[:, 'C'] = df[args.confounder]
    else:
        df.loc[:, 'C'] = 0

    # Rename the text column
    df.loc[:, 'text'] = df[args.text]

    # Split into train and test
    logging.info("Splitting into train and test...")
    train = df.query("split == 'train'")
    dev = df.query("split == 'dev'")
    test = df.query("split == 'test'")

    cb = CausalBertWrapper(batch_size=2, g_weight=0.1, Q_weight=0.1, mlm_weight=1)
    logging.info("Training Sentiment Causal BERT for {} epoch(s)...".format(args.epochs))
    train_losses, dev_losses = cb.train(train, dev, epochs=args.epochs)

    train_fig \
        = train_losses.plot(x='epoch', y=['mlm', 'g', 'Q', 'total'], title='Training losses over epochs').get_figure()
    train_fig.savefig("{}_training_losses.png".format(args.experiment))

    dev_fig \
        = dev_losses.plot(x='epoch', y=['mlm', 'g', 'Q', 'total'], title='Dev losses over epochs').get_figure()
    dev_fig.savefig("{}_dev_losses.png".format(args.experiment))

    logging.info("Calculating ATT with inferred treatments...")
    att = cb.ATT(test['C'], test['text'], platt_scaling=True)
    logging.info("ATT = {}".format(att))

    logging.info("Calculating ATT with ground-truth treatments...")
    att = cb.ATT(test['C'], test['text'], T=test['T'], platt_scaling=True)
    logging.info("ATT = {}".format(att))

    logging.info("Calculating ATE...")
    ate = cb.ATE(test['C'], test['text'], platt_scaling=True)
    logger.info("ATE = {}".format(ate))


if __name__ == '__main__':
    main()
