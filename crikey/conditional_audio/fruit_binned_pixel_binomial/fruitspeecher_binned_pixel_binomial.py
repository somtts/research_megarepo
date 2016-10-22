import numpy as np
import theano
from theano import tensor
from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams
from scipy.io import wavfile
import os
import sys
from kdllib import load_checkpoint, theano_one_hot, concatenate
from kdllib import fetch_fruitspeech_spectrogram, list_iterator
from kdllib import np_zeros, GRU, GRUFork, dense_to_one_hot
from kdllib import make_weights, make_biases, relu, run_loop
from kdllib import as_shared, adam, gradient_clipping
from kdllib import get_values_from_function, set_shared_variables_in_function
from kdllib import soundsc, categorical_crossentropy
from kdllib import sample_binomial, sigmoid



if __name__ == "__main__":
    import argparse

    speech = fetch_fruitspeech_spectrogram()
    X = speech["data"]
    y = speech["target"]
    vocabulary = speech["vocabulary"]
    vocabulary_size = speech["vocabulary_size"]
    reconstruct = speech["reconstruct"]
    fs = speech["sample_rate"]
    X = np.array([x.astype(theano.config.floatX) for x in X])
    y = np.array([yy.astype(theano.config.floatX) for yy in y])

    minibatch_size = 1
    n_epochs = 200  # Used way at the bottom in the training loop!
    checkpoint_every_n = 10
    cut_len = 41  # Used way at the bottom in the training loop!
    random_state = np.random.RandomState(1999)

    train_itr = list_iterator([X, y], minibatch_size, axis=1,
                              stop_index=105, randomize=True, make_mask=True)
    valid_itr = list_iterator([X, y], minibatch_size, axis=1,
                              start_index=100, randomize=True, make_mask=True)
    X_mb, X_mb_mask, c_mb, c_mb_mask = next(train_itr)
    train_itr.reset()

    n_hid = 256
    att_size = 10
    n_proj = 256
    n_v_proj = 5
    n_bins = 10
    input_dim = X_mb.shape[-1]
    n_pred_proj = 1

    n_feats = X_mb.shape[-1]
    n_chars = vocabulary_size
    # n_components = 3
    # n_density = 2 * n_out * n_components + n_components

    desc = "Speech generation"
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument('-s', '--sample',
                        help='Sample from a checkpoint file',
                        default=None,
                        required=False)
    parser.add_argument('-p', '--plot',
                        help='Plot training curves from a checkpoint file',
                        default=None,
                        required=False)
    parser.add_argument('-w', '--write',
                        help='The string to write out (default first minibatch)',
                        default=None,
                        required=False)

    def restricted_int(x):
        if x is None:
            # None makes it "auto" sample
            return x
        x = int(x)
        if x < 1:
            raise argparse.ArgumentTypeError("%r not range [1, inf]" % (x,))
        return x
    parser.add_argument('-sl', '--sample_length',
                        help='Number of steps to sample, default is automatic',
                        type=restricted_int,
                        default=None,
                        required=False)
    parser.add_argument('-c', '--continue', dest="cont",
                        help='Continue training from another saved model',
                        default=None,
                        required=False)
    args = parser.parse_args()
    if args.plot is not None or args.sample is not None:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        if args.sample is not None:
            checkpoint_file = args.sample
        else:
            checkpoint_file = args.plot
        if not os.path.exists(checkpoint_file):
            raise ValueError("Checkpoint file path %s" % checkpoint_file,
                             " does not exist!")
        print(checkpoint_file)
        checkpoint_dict = load_checkpoint(checkpoint_file)
        train_costs = checkpoint_dict["train_costs"]
        valid_costs = checkpoint_dict["valid_costs"]
        plt.plot(train_costs)
        plt.plot(valid_costs)
        plt.savefig("costs.png")

        X_mb, X_mb_mask, c_mb, c_mb_mask = next(valid_itr)
        valid_itr.reset()
        prev_h1, prev_h2, prev_h3 = [np_zeros((minibatch_size, n_hid))
                                     for i in range(3)]
        prev_kappa = np_zeros((minibatch_size, att_size))
        prev_w = np_zeros((minibatch_size, n_chars))
        if args.sample is not None:
            predict_function = checkpoint_dict["predict_function"]
            attention_function = checkpoint_dict["attention_function"]
            sample_function = checkpoint_dict["sample_function"]
            if args.write is not None:
                sample_string = args.write
                print("Sampling using sample string %s" % sample_string)
                oh = dense_to_one_hot(
                    np.array([vocabulary[c] for c in sample_string]),
                    vocabulary_size)
                c_mb = np.zeros(
                    (len(oh), minibatch_size, oh.shape[-1])).astype(c_mb.dtype)
                c_mb[:len(oh), :, :] = oh[:, None, :]
                c_mb = c_mb[:len(oh)]
                c_mb_mask = np.ones_like(c_mb[:, :, 0])

            if args.sample_length is None:
                raise ValueError("NYI - use -sl or --sample_length ")
            else:
                fixed_steps = args.sample_length
                completed = []
                init_x = np.zeros_like(X_mb[0])
                for i in range(fixed_steps):
                    rvals = sample_function(init_x, c_mb, c_mb_mask, prev_h1, prev_h2,
                                            prev_h3, prev_kappa, prev_w)
                    sampled, h1_s, h2_s, h3_s, k_s, w_s, stop_s, stop_h = rvals
                    # remove after retraining
                    sampled = sampled.transpose(1, 0)
                    completed.append(sampled)
                    # cheating sampling...
                    #init_x = X_mb[i]
                    init_x = sampled
                    prev_h1 = h1_s
                    prev_h2 = h2_s
                    prev_h3 = h3_s
                    prev_kappa = k_s
                    prev_w = w_s
                cond = c_mb
                print("Completed sampling after %i steps" % fixed_steps)
            completed = np.array(completed).transpose(1, 0, 2)
            rlookup = {v: k for k, v in vocabulary.items()}
            all_strings = []
            for yi in y:
                ex_str = "".join([rlookup[c]
                                  for c in np.argmax(yi, axis=1)])
                all_strings.append(ex_str)
            for i in range(len(completed)):
                ex = completed[i]
                ex_str = "".join([rlookup[c]
                                  for c in np.argmax(cond[:, i], axis=1)])
                s = "gen_%s_%i.wav" % (ex_str, i)
                ii = reconstruct(ex)
                wavfile.write(s, fs, soundsc(ii))
                if ex_str in all_strings:
                    inds = [n for n, s in enumerate(all_strings)
                            if ex_str == s]
                    ind = inds[0]
                    it = reconstruct(X[ind])
                    s = "orig_%s_%i.wav" % (ex_str, i)
                    wavfile.write(s, fs, soundsc(it))
        valid_itr.reset()
        print("Sampling complete, exiting...")
        sys.exit()
    else:
        print("No plotting arguments, starting training mode!")

    X_sym = tensor.tensor3("X_sym")
    X_sym.tag.test_value = X_mb
    X_mask_sym = tensor.matrix("X_mask_sym")
    X_mask_sym.tag.test_value = X_mb_mask
    c_sym = tensor.tensor3("c_sym")
    c_sym.tag.test_value = c_mb
    c_mask_sym = tensor.matrix("c_mask_sym")
    c_mask_sym.tag.test_value = c_mb_mask

    init_h1 = tensor.matrix("init_h1")
    init_h1.tag.test_value = np_zeros((minibatch_size, n_hid))

    init_h2 = tensor.matrix("init_h2")
    init_h2.tag.test_value = np_zeros((minibatch_size, n_hid))

    init_h3 = tensor.matrix("init_h3")
    init_h3.tag.test_value = np_zeros((minibatch_size, n_hid))

    init_kappa = tensor.matrix("init_kappa")
    init_kappa.tag.test_value = np_zeros((minibatch_size, att_size))

    init_w = tensor.matrix("init_w")
    init_w.tag.test_value = np_zeros((minibatch_size, n_chars))

    params = []
    biases = []

    cell1 = GRU(input_dim, n_hid, random_state)
    cell2 = GRU(n_hid, n_hid, random_state)
    cell3 = GRU(n_hid, n_hid, random_state)

    params += cell1.get_params()
    params += cell2.get_params()
    params += cell3.get_params()

    inp_to_h1 = GRUFork(input_dim, n_hid, random_state)
    inp_to_h2 = GRUFork(input_dim, n_hid, random_state)
    inp_to_h3 = GRUFork(input_dim, n_hid, random_state)
    att_to_h1 = GRUFork(n_chars, n_hid, random_state)
    att_to_h2 = GRUFork(n_chars, n_hid, random_state)
    att_to_h3 = GRUFork(n_chars, n_hid, random_state)
    h1_to_h2 = GRUFork(n_hid, n_hid, random_state)
    h1_to_h3 = GRUFork(n_hid, n_hid, random_state)
    h2_to_h3 = GRUFork(n_hid, n_hid, random_state)

    params += inp_to_h1.get_params()
    params += inp_to_h2.get_params()
    params += inp_to_h3.get_params()
    params += att_to_h1.get_params()
    params += att_to_h2.get_params()
    params += att_to_h3.get_params()
    params += h1_to_h2.get_params()
    params += h1_to_h3.get_params()
    params += h2_to_h3.get_params()

    biases += inp_to_h1.get_biases()
    biases += inp_to_h2.get_biases()
    biases += inp_to_h3.get_biases()
    biases += att_to_h1.get_biases()
    biases += att_to_h2.get_biases()
    biases += att_to_h3.get_biases()
    biases += h1_to_h2.get_biases()
    biases += h1_to_h3.get_biases()
    biases += h2_to_h3.get_biases()

    # 3 to include groundtruth, pixel RNN style
    outs_to_v_h1 = GRUFork(3, n_v_proj, random_state)
    params += outs_to_v_h1.get_params()
    biases += outs_to_v_h1.get_biases()

    v_cell1 = GRU(n_v_proj, n_v_proj, random_state)
    params += v_cell1.get_params()

    h1_to_att_a, h1_to_att_b, h1_to_att_k = make_weights(n_hid, 3 * [att_size],
                                                         random_state)
    h1_to_outs, = make_weights(n_hid, [n_proj], random_state)
    h2_to_outs, = make_weights(n_hid, [n_proj], random_state)
    h3_to_outs, = make_weights(n_hid, [n_proj], random_state)

    params += [h1_to_att_a, h1_to_att_b, h1_to_att_k]
    params += [h1_to_outs, h2_to_outs, h3_to_outs]

    pred_proj, = make_weights(n_v_proj, [n_pred_proj], random_state)
    pred_b, = make_biases([n_pred_proj])

    params += [pred_proj, pred_b]
    biases += [pred_b]

    inpt = X_sym[:-1]
    target = X_sym[1:]
    mask = X_mask_sym[1:]
    context = c_sym * c_mask_sym.dimshuffle(0, 1, 'x')

    inp_h1, inpgate_h1 = inp_to_h1.proj(inpt)
    inp_h2, inpgate_h2 = inp_to_h2.proj(inpt)
    inp_h3, inpgate_h3 = inp_to_h3.proj(inpt)

    u = tensor.arange(c_sym.shape[0]).dimshuffle('x', 'x', 0)
    u = tensor.cast(u, theano.config.floatX)

    def calc_phi(k_t, a_t, b_t, u_c):
        a_t = a_t.dimshuffle(0, 1, 'x')
        b_t = b_t.dimshuffle(0, 1, 'x')
        ss1 = (k_t.dimshuffle(0, 1, 'x') - u_c) ** 2
        ss2 = -b_t * ss1
        ss3 = a_t * tensor.exp(ss2)
        ss4 = ss3.sum(axis=1)
        return ss4

    def step(xinp_h1_t, xgate_h1_t,
             xinp_h2_t, xgate_h2_t,
             xinp_h3_t, xgate_h3_t,
             h1_tm1, h2_tm1, h3_tm1,
             k_tm1, w_tm1, ctx):

        attinp_h1, attgate_h1 = att_to_h1.proj(w_tm1)

        h1_t = cell1.step(xinp_h1_t + attinp_h1, xgate_h1_t + attgate_h1,
                          h1_tm1)
        h1inp_h2, h1gate_h2 = h1_to_h2.proj(h1_t)
        h1inp_h3, h1gate_h3 = h1_to_h3.proj(h1_t)

        a_t = h1_t.dot(h1_to_att_a)
        b_t = h1_t.dot(h1_to_att_b)
        k_t = h1_t.dot(h1_to_att_k)

        a_t = tensor.exp(a_t)
        b_t = tensor.exp(b_t)
        k_t = k_tm1 + tensor.exp(k_t)

        ss4 = calc_phi(k_t, a_t, b_t, u)
        ss5 = ss4.dimshuffle(0, 1, 'x')
        ss6 = ss5 * ctx.dimshuffle(1, 0, 2)
        w_t = ss6.sum(axis=1)

        attinp_h2, attgate_h2 = att_to_h2.proj(w_t)
        attinp_h3, attgate_h3 = att_to_h3.proj(w_t)

        h2_t = cell2.step(xinp_h2_t + h1inp_h2 + attinp_h2,
                          xgate_h2_t + h1gate_h2 + attgate_h2, h2_tm1)

        h2inp_h3, h2gate_h3 = h2_to_h3.proj(h2_t)

        h3_t = cell3.step(xinp_h3_t + h1inp_h3 + h2inp_h3 + attinp_h3,
                          xgate_h3_t + h1gate_h3 + h2gate_h3 + attgate_h3,
                          h3_tm1)
        return h1_t, h2_t, h3_t, k_t, w_t


    init_x = tensor.fmatrix()
    init_x.tag.test_value = np_zeros((minibatch_size, n_feats)).astype(theano.config.floatX)
    srng = RandomStreams(1999)

    # Used to calculate stopping heuristic from sections 5.3
    u_max = 0. * tensor.arange(c_sym.shape[0]) + c_sym.shape[0]
    u_max = u_max.dimshuffle('x', 'x', 0)
    u_max = tensor.cast(u_max, theano.config.floatX)
    def sample_step(x_tm1, h1_tm1, h2_tm1, h3_tm1, k_tm1, w_tm1, ctx):
        xinp_h1_t, xgate_h1_t = inp_to_h1.proj(x_tm1)
        xinp_h2_t, xgate_h2_t = inp_to_h2.proj(x_tm1)
        xinp_h3_t, xgate_h3_t = inp_to_h3.proj(x_tm1)

        attinp_h1, attgate_h1 = att_to_h1.proj(w_tm1)

        h1_t = cell1.step(xinp_h1_t + attinp_h1, xgate_h1_t + attgate_h1,
                          h1_tm1)
        h1inp_h2, h1gate_h2 = h1_to_h2.proj(h1_t)
        h1inp_h3, h1gate_h3 = h1_to_h3.proj(h1_t)

        a_t = h1_t.dot(h1_to_att_a)
        b_t = h1_t.dot(h1_to_att_b)
        k_t = h1_t.dot(h1_to_att_k)

        a_t = tensor.exp(a_t)
        b_t = tensor.exp(b_t)
        k_t = k_tm1 + tensor.exp(k_t)

        ss_t = calc_phi(k_t, a_t, b_t, u)
        # calculate and return stopping criteria
        sh_t = calc_phi(k_t, a_t, b_t, u_max)
        ss5 = ss_t.dimshuffle(0, 1, 'x')
        ss6 = ss5 * ctx.dimshuffle(1, 0, 2)
        w_t = ss6.sum(axis=1)

        attinp_h2, attgate_h2 = att_to_h2.proj(w_t)
        attinp_h3, attgate_h3 = att_to_h3.proj(w_t)

        h2_t = cell2.step(xinp_h2_t + h1inp_h2 + attinp_h2,
                          xgate_h2_t + h1gate_h2 + attgate_h2, h2_tm1)

        h2inp_h3, h2gate_h3 = h2_to_h3.proj(h2_t)

        h3_t = cell3.step(xinp_h3_t + h1inp_h3 + h2inp_h3 + attinp_h3,
                          xgate_h3_t + h1gate_h3 + h2gate_h3 + attgate_h3,
                          h3_tm1)
        out_t = h1_t.dot(h1_to_outs) + h2_t.dot(h2_to_outs) + h3_t.dot(
            h3_to_outs)
        theano.printing.Print("out_t.shape")(out_t.shape)
        out_t_shape = out_t.shape
        x_tm1_shuf = x_tm1.dimshuffle(1, 0, 'x')
        vinp_t = out_t.dimshuffle(1, 0, 'x')
        theano.printing.Print("x_tm1.shape")(x_tm1.shape)
        theano.printing.Print("vinp_t.shape")(vinp_t.shape)
        init_pred = tensor.zeros((vinp_t.shape[1],), dtype=theano.config.floatX)
        init_hidden = tensor.zeros((x_tm1_shuf.shape[1], n_v_proj),
                                    dtype=theano.config.floatX)

        def sample_out_step(x_tm1_shuf, vinp_t, pred_fm1, v_h1_tm1):
            j_t = concatenate((x_tm1_shuf, vinp_t,
                               pred_fm1.dimshuffle(0, 'x')),
                               axis=-1)
            theano.printing.Print("j_t.shape")(j_t.shape)
            vinp_h1_t, vgate_h1_t = outs_to_v_h1.proj(j_t)
            v_h1_t = v_cell1.step(vinp_h1_t, vgate_h1_t, v_h1_tm1)
            theano.printing.Print("v_h1_t.shape")(v_h1_t.shape)
            pred_f = sigmoid(v_h1_t.dot(pred_proj) + pred_b)
            pred_f = sample_binomial(pred_f, n_bins, srng)
            theano.printing.Print("pred_f.shape")(pred_f.shape)
            return pred_f[:, 0], v_h1_t

        r, isupdates = theano.scan(
            fn=sample_out_step,
            sequences=[x_tm1_shuf, vinp_t],
            outputs_info=[init_pred, init_hidden])
        (pred_t, v_h1_t) = r
        theano.printing.Print("pred_t.shape")(pred_t.shape)
        theano.printing.Print("v_h1_t.shape")(v_h1_t.shape)
        #pred_t = sigmoid(pre_pred_t)
        #x_t = sample_binomial(pred_t, n_bins, srng)
        # MSE
        x_t = pred_t
        return x_t, h1_t, h2_t, h3_t, k_t, w_t, ss_t, sh_t, isupdates

    (sampled, h1_s, h2_s, h3_s, k_s, w_s, stop_s, stop_h, supdates) = sample_step(
        init_x, init_h1, init_h2, init_h3, init_kappa, init_w, c_sym)
    sampled = sampled.dimshuffle(1, 0)
    theano.printing.Print("sampled.shape")(sampled.shape)

    (h1, h2, h3, kappa, w), updates = theano.scan(
        fn=step,
        sequences=[inp_h1, inpgate_h1,
                   inp_h2, inpgate_h2,
                   inp_h3, inpgate_h3],
        outputs_info=[init_h1, init_h2, init_h3, init_kappa, init_w],
        non_sequences=[context])

    outs = h1.dot(h1_to_outs) + h2.dot(h2_to_outs) + h3.dot(h3_to_outs)
    outs_shape = outs.shape
    theano.printing.Print("outs.shape")(outs.shape)
    outs = outs.dimshuffle(2, 1, 0)
    vinp = outs.reshape((outs_shape[2], -1, 1))
    theano.printing.Print("vinp.shape")(vinp.shape)
    shp = vinp.shape

    shuff_inpt_shapes = inpt.shape
    theano.printing.Print("inpt.shape")(inpt.shape)
    shuff_inpt = inpt.dimshuffle(2, 1, 0)
    theano.printing.Print("shuff_inpt.shape")(shuff_inpt.shape)
    shuff_inpt = shuff_inpt.reshape((shuff_inpt_shapes[2],
                                     shuff_inpt_shapes[1] * shuff_inpt_shapes[0],
                                     1))

    theano.printing.Print("shuff_inpt.shape")(shuff_inpt.shape)
    theano.printing.Print("vinp.shape")(vinp.shape)
    # input from previous time, pred from previous feature
    """
    dimshuffle hacks and [:, 0] to avoid this error:
    TypeError: Inconsistency in the inner graph of scan 'scan_fn' : an input
    and an output are associated with the same recurrent state and should have
    the same type but have type 'TensorType(float32, col)' and
    'TensorType(float32, matrix)' respectively.
    """
    j = concatenate((shuff_inpt, vinp), axis=-1)
    raise ValueError()
    def out_step(shuff_inpt_tm1, vinp_t, pred_fm1, v_h1_tm1):
        j_t = concatenate((shuff_inpt_tm1, vinp_t, pred_fm1.dimshuffle(0, 'x')),
                           axis=-1)
        theano.printing.Print("j_t.shape")(j_t.shape)
        vinp_h1_t, vgate_h1_t = outs_to_v_h1.proj(j_t)
        v_h1_t = v_cell1.step(vinp_h1_t, vgate_h1_t, v_h1_tm1)
        theano.printing.Print("v_h1_t.shape")(v_h1_t.shape)
        pred_f = sigmoid(v_h1_t.dot(pred_proj) + pred_b)
        # MLE
        pred_f = n_bins * pred_f
        theano.printing.Print("pred_f.shape")(pred_f.shape)
        return pred_f[:, 0], v_h1_t

    init_pred = tensor.zeros((vinp.shape[1],), dtype=theano.config.floatX)
    init_hidden = tensor.zeros((shuff_inpt.shape[1], n_v_proj),
                                dtype=theano.config.floatX)
    theano.printing.Print("init_pred.shape")(init_pred.shape)
    theano.printing.Print("init_hidden.shape")(init_hidden.shape)
    r, updates = theano.scan(
        fn=out_step,
        sequences=[shuff_inpt, vinp],
        outputs_info=[init_pred, init_hidden])
    (pred, v_h1) = r
    pred = pred.dimshuffle(1, 'x', 0)
    v_h1 = v_h1.dimshuffle(2, 1, 0)
    theano.printing.Print("pred.shape")(pred.shape)
    theano.printing.Print("v_h1.shape")(v_h1.shape)
    # binomial
    #cost = -target * tensor.log(pred) - (n_bins - target) * tensor.log(1 - pred)
    # MSE
    cost = (pred - target) ** 2
    theano.printing.Print("pred.shape")(pred.shape)
    theano.printing.Print("target.shape")(target.shape)

    cost = cost * mask.dimshuffle(0, 1, 'x')
    # sum over sequence length and features, mean over minibatch
    cost = cost.dimshuffle(0, 2, 1)
    cost = cost.reshape((-1, cost.shape[2]))
    cost = cost.sum(axis=0).mean()

    l2_penalty = 0
    for p in list(set(params) - set(biases)):
        l2_penalty += (p ** 2).sum()

    cost = cost + 1E-3 * l2_penalty
    grads = tensor.grad(cost, params)
    grads = gradient_clipping(grads, 10.)

    learning_rate = 1E-4

    opt = adam(params, learning_rate)
    updates = opt.updates(params, grads)

    if args.cont is not None:
        print("Continuing training from saved model")
        continue_path = args.cont
        if not os.path.exists(continue_path):
            raise ValueError("Continue model %s, path not "
                             "found" % continue_path)
        saved_checkpoint = load_checkpoint(continue_path)
        checkpoint_dict = saved_checkpoint
        train_function = checkpoint_dict["train_function"]
        cost_function = checkpoint_dict["cost_function"]
        predict_function = checkpoint_dict["predict_function"]
        attention_function = checkpoint_dict["attention_function"]
        sample_function = checkpoint_dict["sample_function"]
        """
        trained_weights = get_values_from_function(
            saved_checkpoint["train_function"])
        set_shared_variables_in_function(train_function, trained_weights)
        """
    else:
        train_function = theano.function([X_sym, X_mask_sym, c_sym, c_mask_sym,
                                          init_h1, init_h2, init_h3, init_kappa,
                                          init_w],
                                         [cost, h1, h2, h3, kappa, w],
                                         updates=updates)
        cost_function = theano.function([X_sym, X_mask_sym, c_sym, c_mask_sym,
                                         init_h1, init_h2, init_h3, init_kappa,
                                         init_w],
                                        [cost, h1, h2, h3, kappa, w])
        predict_function = theano.function([X_sym, X_mask_sym, c_sym, c_mask_sym,
                                            init_h1, init_h2, init_h3, init_kappa,
                                            init_w],
                                           [outs],
                                           on_unused_input='warn')
        attention_function = theano.function([X_sym, X_mask_sym, c_sym, c_mask_sym,
                                              init_h1, init_h2, init_h3, init_kappa,
                                              init_w],
                                             [kappa, w], on_unused_input='warn')
        sample_function = theano.function([init_x, c_sym, c_mask_sym, init_h1, init_h2,
                                           init_h3, init_kappa, init_w],
                                          [sampled, h1_s, h2_s, h3_s, k_s, w_s,
                                           stop_s, stop_h],
                                          on_unused_input="warn",
                                          updates=supdates)
        print("Beginning training loop")
        checkpoint_dict = {}
        checkpoint_dict["train_function"] = train_function
        checkpoint_dict["cost_function"] = cost_function
        checkpoint_dict["predict_function"] = predict_function
        checkpoint_dict["attention_function"] = attention_function
        checkpoint_dict["sample_function"] = sample_function


    def _loop(function, itr):
        prev_h1, prev_h2, prev_h3 = [np_zeros((minibatch_size, n_hid))
                                     for i in range(3)]
        prev_kappa = np_zeros((minibatch_size, att_size))
        prev_w = np_zeros((minibatch_size, n_chars))
        X_mb, X_mb_mask, c_mb, c_mb_mask = next(itr)
        n_cuts = len(X_mb) // cut_len + 1
        partial_costs = []
        for n in range(n_cuts):
            start = n * cut_len
            stop = (n + 1) * cut_len
            if len(X_mb[start:stop]) < cut_len:
                new_len = cut_len - len(X_mb) % cut_len
                zeros = np.zeros((new_len, X_mb.shape[1],
                                  X_mb.shape[2]))
                zeros = zeros.astype(X_mb.dtype)
                mask_zeros = np.zeros((new_len, X_mb_mask.shape[1]))
                mask_zeros = mask_zeros.astype(X_mb_mask.dtype)
                X_mb = np.concatenate((X_mb, zeros), axis=0)
                X_mb_mask = np.concatenate((X_mb_mask, mask_zeros), axis=0)
                assert len(X_mb[start:stop]) == cut_len
                assert len(X_mb_mask[start:stop]) == cut_len
            rval = function(X_mb[start:stop],
                            X_mb_mask[start:stop],
                            c_mb, c_mb_mask,
                            prev_h1, prev_h2, prev_h3, prev_kappa, prev_w)
            current_cost = rval[0]
            prev_h1, prev_h2, prev_h3 = rval[1:4]
            prev_h1 = prev_h1[-1]
            prev_h2 = prev_h2[-1]
            prev_h3 = prev_h3[-1]
            prev_kappa = rval[4][-1]
            prev_w = rval[5][-1]
        partial_costs.append(current_cost)
        return partial_costs

run_loop(_loop, train_function, train_itr, cost_function, valid_itr,
         n_epochs=n_epochs, checkpoint_dict=checkpoint_dict,
         checkpoint_every_n=checkpoint_every_n, skip_minimums=True)
