import logging
import math
import chainer
import torch
from torch import nn
from torch.nn import LayerNorm

from espnet.asr import asr_utils
from espnet.nets.beam_search import BeamSearch


class MultiSequential(torch.nn.Sequential):
    def forward(self, *args):
        for m in self:
            args = m(*args)
        return args


def repeat(N, fn):
    """repeat module N times
    :param int N: repeat time
    :param function fn: function to generate module
    :return: repeated modules
    :rtype: MultiSequential
    """
    return MultiSequential(*[fn() for _ in range(N)])


class EncoderLayer(nn.Module):
    def __init__(self, size, self_attn, feed_forward, dropout):
        super(EncoderLayer, self).__init__()
        self.self_attn = self_attn
        self.feed_forward = feed_forward
        self.norm1 = LayerNorm(size)
        self.norm2 = LayerNorm(size)
        self.dropout = nn.Dropout(dropout)
        self.size = size

    def forward(self, x, mask):
        """Compute encoded features
        :param torch.Tensor x: encoded source features (batch, max_time_in, size)
        :param torch.Tensor mask: mask for x (batch, max_time_in)
        """
        nx = self.norm1(x)
        x = x + self.dropout(self.self_attn(nx, nx, nx, mask))
        nx = self.norm2(x)
        return x + self.dropout(self.feed_forward(nx)), mask


class DecoderLayer(nn.Module):
    def __init__(self, size, self_attn, src_attn, feed_forward, dropout):
        super(DecoderLayer, self).__init__()
        self.size = size
        self.self_attn = self_attn
        self.src_attn = src_attn
        self.feed_forward = feed_forward
        self.norm1 = LayerNorm(size)
        self.norm2 = LayerNorm(size)
        self.norm3 = LayerNorm(size)
        self.dropout = nn.Dropout(dropout)

    def forward(self, tgt, tgt_mask, memory, memory_mask):
        """Compute decoded features
        :param torch.Tensor tgt: decoded previous target features (batch, max_time_out, size)
        :param torch.Tensor tgt_mask: mask for x (batch, max_time_out)
        :param torch.Tensor memory: encoded source features (batch, max_time_in, size)
        :param torch.Tensor memory_mask: mask for memory (batch, max_time_in)
        """
        x = tgt
        nx = self.norm1(x)
        x = x + self.dropout(self.self_attn(nx, nx, nx, tgt_mask))
        nx = self.norm2(x)
        x = x + self.dropout(self.src_attn(nx, memory, memory, memory_mask))
        nx = self.norm3(x)
        return x + self.dropout(self.feed_forward(nx)), tgt_mask, memory, memory_mask


def subsequent_mask(size, device="cpu", dtype=torch.uint8):
    """Create mask for subsequent steps (1, size, size)
    :param int size: size of mask
    :param str device: "cpu" or "cuda" or torch.Tensor.device
    :param torch.dtype dtype: result dtype
    :rtype: torch.Tensor
    >>> subsequent_mask(3)
    [[[1, 1, 1],
      [0, 1, 1],
      [0, 0, 1]]]
    """
    ret = torch.ones(size, size, device=device, dtype=dtype)
    return torch.triu(ret, out=ret).unsqueeze(0)


import numpy
MIN_VALUE = float(numpy.finfo(numpy.float32).min)


class MultiHeadedAttention(nn.Module):
    def __init__(self, h, d_model, dropout):
        super(MultiHeadedAttention, self).__init__()
        assert d_model % h == 0
        # We assume d_v always equals d_k
        self.d_k = d_model // h
        self.h = h
        self.linear_q = nn.Linear(d_model, d_model)
        self.linear_k = nn.Linear(d_model, d_model)
        self.linear_v = nn.Linear(d_model, d_model)
        self.linear_out = nn.Linear(d_model, d_model)
        self.attn = None
        self.dropout = nn.Dropout(p=dropout)

    def forward(self, query, key, value, mask):
        """Compute 'Scaled Dot Product Attention'
        :param torch.Tensor query: (batch, time1, size)
        :param torch.Tensor key: (batch, time2, size)
        :param torch.Tensor value: (batch, time2, size)
        :param torch.Tensor mask: (batch, time1)
        :param torch.nn.Dropout dropout:
        :return torch.Tensor: attentined and transformed `value` (batch, time1, d_model)
             weighted by the query dot key attention (batch, head, time1, time2)
        """
        n_batch = query.size(0)
        # (batch, head, time1/2, d_k)
        q = self.linear_q(query).view(n_batch, self.h, -1, self.d_k)
        k = self.linear_k(key).view(n_batch, self.h, -1, self.d_k)
        v = self.linear_v(value).view(n_batch, self.h, -1, self.d_k)

        scores = torch.matmul(q, k.transpose(-2, -1)) / math.sqrt(self.d_k)
        if mask is not None:
            mask = mask.unsqueeze(1)
            scores = scores.masked_fill(mask == 0, MIN_VALUE)
        self.attn = torch.softmax(scores, dim = -1)

        p_attn = self.dropout(self.attn)
        x = torch.matmul(p_attn, v)
        x = x.transpose(1, 2).contiguous().view(n_batch, -1, self.h * self.d_k)
        return self.linear_out(x)


class PositionwiseFeedForward(nn.Module):
    def __init__(self, d_model, d_ff, dropout):
        super(PositionwiseFeedForward, self).__init__()
        self.w_1 = nn.Linear(d_model, d_ff)
        self.w_2 = nn.Linear(d_ff, d_model)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        return self.w_2(self.dropout(torch.relu(self.w_1(x))))


class PositionalEncoding(nn.Module):
    def __init__(self, d_model, dropout, max_len=5000):
        super(PositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(p=dropout)
        # Compute the positional encodings once in log space.
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float32).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2, dtype=torch.float32) *
                             -(math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0)
        self.xscale = math.sqrt(d_model)
        self.register_buffer('pe', pe)

    def forward(self, x):
        with torch.no_grad():
            x = x * self.xscale + self.pe[:, :x.size(1)]
            return self.dropout(x)


class Encoder(torch.nn.Module):
    def __init__(self, idim, args):
        super(Encoder, self).__init__()
        self.input_layer = torch.nn.Sequential(
            torch.nn.Linear(idim, args.adim),
            torch.nn.Dropout(args.dropout_rate),
            torch.nn.ReLU()
        )
        self.encoders = repeat(
            args.elayers,
            lambda : EncoderLayer(
                args.adim,
                MultiHeadedAttention(args.aheads, args.adim, args.dropout_rate),
                PositionwiseFeedForward(args.adim, args.eunits, args.dropout_rate),
                args.dropout_rate
            )
        )
        self.norm = LayerNorm(args.adim)

    def forward(self, x, mask):
        x = self.input_layer(x)
        return self.encoders(x, mask)


class Decoder(torch.nn.Module, ScoringBase):
    def __init__(self, odim, args):
        super(Decoder, self).__init__()
        self.embed = torch.nn.Sequential(
            torch.nn.Embedding(odim, args.adim),
            PositionalEncoding(args.adim, args.dropout_rate)
        )
        self.decoders = repeat(
            args.dlayers,
            lambda : DecoderLayer(
                args.adim,
                MultiHeadedAttention(args.aheads, args.adim, args.dropout_rate),
                MultiHeadedAttention(args.aheads, args.adim, args.dropout_rate),
                PositionwiseFeedForward(args.adim, args.dunits, args.dropout_rate),
                args.dropout_rate
            )
        )
        self.output_norm = LayerNorm(args.adim)
        self.output_layer = torch.nn.Linear(args.adim, odim)

    def forward(self, tgt, tgt_mask, memory, memory_mask):
        x = self.embed(tgt)
        x, tgt_mask, memory, memory_mask = self.decoders(x, tgt_mask, memory, memory_mask)
        x = self.output_layer(self.output_norm(x))
        return x, tgt_mask

    def score(self, token, enc_output, state):
        y, _ = self.forward(token, None, *enc_output)
        return torch.log_softmax(y[:, -1, :])


class E2E(torch.nn.Module):
    def __init__(self, idim, odim, args):
        super(E2E, self).__init__()
        self.encoder = Encoder(idim, args)
        self.decoder = Decoder(odim, args)
        self.sos = odim - 1
        self.eos = odim - 1
        self.odim = odim
        self.ignore_id = -1
        self.subsample = [0]
        # self.char_list = args.char_list
        # self.verbose = args.verbose
        self.reset_parameters(args)

    def reset_parameters(self, args):
        if args.ninit == "none":
            return
        # weight init
        for p in self.parameters():
            if p.dim() > 1:
                if args.ninit == "chainer":
                    stdv = 1. / math.sqrt(p.data.size(1))
                    p.data.normal_(0, stdv)
                elif args.ninit == "xavier_uniform":
                    torch.nn.init.xavier_uniform_(p.data)
                elif args.ninit == "xavier_normal":
                    torch.nn.init.xavier_normal_(p.data)
                elif args.ninit == "kaiming_uniform":
                    torch.nn.init.kaiming_uniform_(p.data, nonlinearity="relu")
                elif args.ninit == "kaiming_normal":
                    torch.nn.init.kaiming_normal_(p.data, nonlinearity="relu")
                else:
                    raise ValueError("Unknown initialization: " + args.ninit)
        # bias init
        for p in self.parameters():
            if p.dim() == 1:
                p.data.zero_()
        # embedding init
        self.decoder.embed[0].weight.data.normal_(0, 1)

    def add_sos_eos(self, ys_pad):
        from espnet.nets.e2e_asr_th import pad_list
        eos = ys_pad.new([self.eos])
        sos = ys_pad.new([self.sos])
        ys = [y[y != self.ignore_id] for y in ys_pad]  # parse padded ys
        ys_in = [torch.cat([sos, y], dim=0) for y in ys]
        ys_out = [torch.cat([y, eos], dim=0) for y in ys]
        return pad_list(ys_in, self.eos), pad_list(ys_out, self.ignore_id)

    def forward(self, xs_pad, ilens, ys_pad):
        '''E2E forward

        :param torch.Tensor xs_pad: batch of padded source sequences (B, Tmax, idim)
        :param torch.Tensor ilens: batch of lengths of source sequences (B)
        :param torch.Tensor ys_pad: batch of padded target sequences (B, Lmax)
        :return: ctc loass value
        :rtype: torch.Tensor
        :return: attention loss value
        :rtype: torch.Tensor
        :return: accuracy in attention decoder
        :rtype: float
        '''
        from espnet.nets.e2e_asr_th import make_pad_mask, pad_list, th_accuracy

        # forward encoder
        src_mask = (~make_pad_mask(ilens)).to(xs_pad.device).unsqueeze(-2)
        hs_pad, hs_mask = self.encoder(xs_pad, src_mask)

        # forward decoder
        ys_in_pad, ys_out_pad = self.add_sos_eos(ys_pad)
        ys_mask = ys_in_pad != self.ignore_id
        m = subsequent_mask(ys_mask.size(-1), device=ys_mask.device)
        ys_mask = ys_mask.unsqueeze(-2) & m
        pred_pad, pred_mask = self.decoder(ys_in_pad, ys_mask, hs_pad, hs_mask)

        # compute loss
        loss_att = torch.nn.functional.cross_entropy(
            pred_pad.view(-1, self.odim),
            ys_out_pad.view(-1),
            ignore_index=self.ignore_id,
            size_average=True)
        acc = th_accuracy(pred_pad.view(-1, self.odim), ys_out_pad,
                          ignore_label=self.ignore_id)

        # TODO(karita) show predected text
        # TODO(karita) calculate these stats
        loss_ctc = None
        cer, wer = 0.0, 0.0
        return loss_ctc, loss_att, acc, cer, wer

    def recognize(self, feat, recog_args, char_list=None, rnnlm=None):
        search = BeamSearch(self.encoder, [self.decoder])


        # import six
        # if rnnlm:
        #     logging.warning("rnnlm is not supported now")

        # logging.info('input lengths: ' + str(feat.shape))

        # # forward encoder
        # src = torch.as_tensor(feat).unsqueeze(0)
        # src_mask = torch.ones(*src.shape[:2], dtype=torch.uint8, device=src.device)
        # h, h_mask = self.encoder.forward(src, src_mask)
        # h = h.squeeze(0)

        # # search parms
        # beam = recog_args.beam_size
        # penalty = recog_args.penalty
        # ctc_weight = recog_args.ctc_weight

        # if recog_args.maxlenratio == 0:
        #     maxlen = h.shape[0]
        # else:
        #     # maxlen >= 1
        #     maxlen = max(1, int(recog_args.maxlenratio * h.size(0)))
        # minlen = int(recog_args.minlenratio * h.size(0))
        # logging.info('max output length: ' + str(maxlen))
        # logging.info('min output length: ' + str(minlen))

        # h = h.unsqueeze(0)
        # y_mask_all = torch.ones(h.size(0), maxlen,
        #                         dtype=torch.uint8, device=src.device)
        # score = 0.0
        # yseq = [self.sos]
        # y = torch.tensor([yseq])
        # global_best_scores = torch.zeros(beam)
        # hyps = [{"score": 0.0, "yseq": [self.sos]}]
        # ended_hyps = []
        # # for i in six.moves.range(maxlen):
        # #     logging.debug('position ' + str(i))

        #     # y_mask = y_mask_all[:, :i+1]
        #     # logging.info("{} {}".format(y.shape, y_mask.shape))
        #     # pred, pred_mask = self.decoder.forward(y, y_mask, h, h_mask)
        #     # log_prob = torch.log_softmax(pred[:, -1, :], dim=-1)
        #     # # (beam/1, odim) -> (beam/1, beam)
        #     # local_best_scores, local_best_ids = log_prob.topk(beam, dim=-1)
        #     # # (beam/1,) -> (beam/1, beam)
        #     # expanded_scores = global_best_scores.unsqueeze(1) + local_best_scores
        #     # # (beam/1, beam) -> (beam,)
        #     # global_best_scores, expanded_ids = expanded_scores.view(-1).topk(beam, dim=0)
        #     # global_best_ids = local_best_ids.view(-1)[expanded_ids]
        #     # global_prev_id = global_best_ids // beam
        #     # global_next_id = expa % beam
        #     # logging.info(global_prev_id)
        #     # logging.info(global_next_id)
        #     # # update hypothesis (beam, i) -> (beam, i+1)
        #     # y = torch.cat((y[global_prev_id], global_next_id.unsqueeze(1)), dim=1)

    def calculate_all_attentions(self, xs_pad, ilens, ys_pad):
        '''E2E attention calculation
        :param torch.Tensor xs_pad: batch of padded input sequences (B, Tmax, idim)
        :param torch.Tensor ilens: batch of lengths of input sequences (B)
        :param torch.Tensor ys_pad: batch of padded character id sequence tensor (B, Lmax)
        :return: attention weights with the following shape,
            1) multi-head case => attention weights (B, H, Lmax, Tmax),
            2) other case => attention weights (B, Lmax, Tmax).
        :rtype: float ndarray
        '''
        with torch.no_grad():
            results = self.forward(xs_pad, ilens, ys_pad)
        ret = dict()
        for name, m in self.named_modules():
            if isinstance(m, MultiHeadedAttention):
                ret[name] = m.attn
        return ret


def _plot_and_save_attention(att_w, filename):
    # dynamically import matplotlib due to not found error
    import matplotlib.pyplot as plt
    import os
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    aspect = float(att_w.size(1)) / att_w.size(0)
    w, h = plt.figaspect(aspect / len(att_w))
    fig = plt.Figure(figsize=(w, h))
    axes = fig.subplots(1, len(att_w))
    for ax, aw in zip(axes, att_w):
        # plt.subplot(1, len(att_w), h)
        ax.imshow(aw, aspect=aspect) # "auto")
        ax.set_xlabel("Input Index")
        ax.set_ylabel("Output Index")
    fig.tight_layout()
    fig.savefig(filename)


def plot_multi_head_attention(data, attn_dict, outdir, suffix="png"):
    for name, att_ws in attn_dict.items():
        for idx, att_w in enumerate(att_ws):
            filename = "%s/%s.%s.%s" % (
                outdir, data[idx][0], name, suffix)
            dec_len = int(data[idx][1]['output'][0]['shape'][0])
            enc_len = int(data[idx][1]['input'][0]['shape'][0])
            if "encoder" in name:
                att_w = att_w[:, :enc_len, :enc_len]
            elif "decoder" in name:
                if "self" in name:
                    att_w = att_w[:, :dec_len, :dec_len]
                else:
                    att_w = att_w[:, :dec_len, :enc_len]
            else:
                logging.warning("unknown name for shaping attention")
            _plot_and_save_attention(att_w, filename)


class PlotAttentionReport(asr_utils.PlotAttentionReport):
    def __call__(self, trainer):
        batch = self.converter([self.converter.transform(self.data)], self.device)
        attn_dict = self.att_vis_fn(*batch)
        suffix = "ep.{.updater.epoch}.png".format(trainer)
        plot_multi_head_attention(self.data, attn_dict, self.outdir, suffix)
