from argparse import Namespace
import logging
import numpy
import torch

import espnet.nets.e2e_asr_transformer_th as T

logging.basicConfig(
    level=logging.INFO, format='%(asctime)s (%(module)s:%(lineno)d) %(levelname)s: %(message)s')


def test_sequential():
    class Masked(torch.nn.Module):
        def forward(self, x, m):
            return x, m

    f = T.MultiSequential(Masked(), Masked())
    x = torch.randn(2, 3)
    m = torch.randn(2, 3) > 0
    assert len(f(x, m)) == 2
    if torch.cuda.is_available():
        f = torch.nn.DataParallel(f)
        f.cuda()
        assert len(f(x.cuda(), m.cuda())) == 2


def subsequent_mask(size):
    # http://nlp.seas.harvard.edu/2018/04/03/attention.html
    "Mask out subsequent positions."
    attn_shape = (1, size, size)
    subsequent_mask = numpy.triu(numpy.ones(attn_shape), k=1).astype('uint8')
    return torch.from_numpy(subsequent_mask) == 0


def test_mask():
    m = T.subsequent_mask(3)
    print(m)
    print(subsequent_mask(3))
    assert (m.unsqueeze(0) == subsequent_mask(3)).all()


def prepare():
    args = Namespace(
        adim=16,
        aheads=4,
        dropout_rate=0.0,
        transformer_attn_dropout_rate=None,
        elayers=3,
        eunits=64,
        dlayers=3,
        dunits=64,
        transformer_init="pytorch",
        transformer_input_layer="linear",
        transformer_length_normalized_loss=True,
        mtlalpha=0.0,
        lsm_weight=0.001
    )
    idim = 83
    odim = 5
    model = T.E2E(idim, odim, args)

    x = torch.randn(5, 70, idim)
    ilens = [70, 50, 30, 30, 20]
    n_token = odim - 1
    y = (torch.rand(5, 10) * n_token % n_token).long()
    olens = [3, 9, 10, 2, 3]
    for i in range(x.size(0)):
        x[i, ilens[i]:] = float("nan")
        y[i, olens[i]:] = model.ignore_id

    data = []
    for i in range(x.size(0)):
        data.append(("utt%d" % i, {
            "input": [{"shape": [ilens[i], idim]}],
            "output": [{"shape": [olens[i]]}]
        }))
    return model, x, torch.tensor(ilens), y, data


def test_transformer_mask():
    model, x, ilens, y, data = prepare()
    # _, loss, _, _, _ = model(x, ilens, y)
    # print(loss)
    # assert not numpy.isnan(float(loss))
    yi, yo = model.add_sos_eos(y)
    y = model.decoder.embed(yi)
    y[0, 3:] = float("nan")
    a = model.decoder.decoders[0].self_attn
    assert not numpy.isnan(a.attn[0, :, :3, :3].detach().numpy()).any()


def test_transformer_synth():
    model, x, ilens, y, data = prepare()

    # test acc is almost 100%
    optim = torch.optim.Adam(model.parameters(), 0.01)
    for i in range(40):
        loss_ctc, loss_att, acc, cer, wer = model(x, ilens, y)
        optim.zero_grad()
        loss_att.backward()
        optim.step()
        print(loss_att, acc)
        # attn_dict = model.calculate_all_attentions(x, ilens, y)
        # T.plot_multi_head_attention(data, attn_dict, "/tmp/espnet-test", "iter%d.png" % i)
    assert acc > 0.8

    # test attention plot
    attn_dict = model.calculate_all_attentions(x[0:1], ilens[0:1], y[0:1])
    T.plot_multi_head_attention(data, attn_dict, "/tmp/espnet-test")

    # test beam search
    recog_args = Namespace(
        beam_size=1,
        penalty=0.0,
        ctc_weight=0.0,
        maxlenratio=0,
        minlenratio=0,
        nbest=1
    )
    with torch.no_grad():
        nbest = model.recognize(x[0, :ilens[0]].numpy(), recog_args)
        print(y[0])
        print(nbest[0]["yseq"][1:-1])


def prepare_copy_task(d_model, d_ff=64, n=1):
    idim = 11
    odim = idim

    if d_model:
        args = Namespace(
            adim=d_model,
            aheads=2,
            dropout_rate=0.1,
            elayers=n,
            eunits=d_ff,
            dlayers=n,
            dunits=d_ff,
            transformer_init="xavier_uniform",
            transformer_input_layer="embed",
            lsm_weight=0.01,
            transformer_attn_dropout_rate=None,
            transformer_length_normalized_loss=True,
            mtlalpha=0.0
        )
        model = T.E2E(idim, odim, args)
    else:
        model = None

    x = torch.randint(1, idim - 1, size=(30, 5)).long()
    ilens = torch.full((x.size(0),), x.size(1)).long()
    data = []
    for i in range(x.size(0)):
        data.append(("utt%d" % i, {
            "input": [{"shape": [ilens[i], idim]}],
            "output": [{"shape": [ilens[i], idim]}]
        }))
    return model, x, ilens, x, data


def test_transformer_copy():
    # copy task defined in http://nlp.seas.harvard.edu/2018/04/03/attention.html#results
    d_model = 32
    model, x, ilens, y, data = prepare_copy_task(d_model)
    model.train()
    if torch.cuda.is_available():
        model.cuda()
    optim = torch.optim.Adam(model.parameters(), 0.01)
    max_acc = 0
    for i in range(1000):
        _, x, ilens, y, data = prepare_copy_task(None)
        if torch.cuda.is_available():
            x = x.cuda()
            y = y.cuda()
        loss_ctc, loss_att, acc, cer, wer = model(x, ilens, y)
        optim.zero_grad()
        loss_att.backward()
        optim.step()
        print(i, loss_att.item(), acc)
        max_acc = max(acc, max_acc)
        # attn_dict = model.calculate_all_attentions(x, ilens, y)
        # T.plot_multi_head_attention(data, attn_dict, "/tmp/espnet-test", "iter%d.png" % i)
    assert max_acc > 0.9

    model.cpu()
    model.eval()
    # test beam search
    recog_args = Namespace(
        beam_size=1,
        penalty=0.0,
        ctc_weight=0.0,
        maxlenratio=0,
        minlenratio=0,
        nbest=1
    )
    if torch.cuda.is_available():
        x = x.cpu()
        y = y.cpu()

    with torch.no_grad():
        print("===== greedy decoding =====")
        for i in range(10):
            nbest = model.recognize(x[i, :ilens[i]].numpy(), recog_args)
            print("gold:", y[i].tolist())
            print("pred:", nbest[0]["yseq"][1:-1])
        print("===== beam search decoding =====")
        recog_args.beam_size = 4
        recog_args.nbest = 4
        for i in range(10):
            nbest = model.recognize(x[i, :ilens[i]].numpy(), recog_args)
            print("gold:", y[i].tolist())
            print("pred:", [n["yseq"][1:-1] for n in nbest])
    # # test attention plot
    # attn_dict = model.calculate_all_attentions(x[:3], ilens[:3], y[:3])
    # T.plot_multi_head_attention(data, attn_dict, "/tmp/espnet-test")
    # assert(False)


def test_transformer_parallel():
    if not torch.cuda.is_available():
        return

    class LossAcc(torch.nn.Module):
        def __init__(self, model):
            super(LossAcc, self).__init__()
            self.model = model

        def forward(self, *args):
            loss_ctc, loss_att, acc, cer, wer = self.model(*args)
            return loss_att, torch.as_tensor(acc).to(loss_att.device)

    model, x, ilens, y, data = prepare()
    model = LossAcc(model)
    model = torch.nn.DataParallel(model).cuda()
    logging.debug(ilens)
    # test acc is almost 100%
    optim = torch.optim.Adam(model.parameters(), 0.02)
    max_acc = 0.0
    for i in range(40):
        loss_att, acc = model(x, torch.as_tensor(ilens), y)
        optim.zero_grad()
        acc = float(acc.mean())
        max_acc = max(acc, max_acc)
        loss_att.mean().backward()
        optim.step()
        print(loss_att, acc)
        # attn_dict = model.calculate_all_attentions(x, ilens, y)
        # T.plot_multi_head_attention(data, attn_dict, "/tmp/espnet-test", "iter%d.png" % i)
    assert max_acc > 0.8


if __name__ == "__main__":
    test_transformer_copy()
