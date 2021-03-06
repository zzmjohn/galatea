__authors__ = "Ian Goodfellow"
__copyright__ = "Copyright 2011, Universite de Montreal"
__credits__ = ["Ian Goodfellow"]
__license__ = "3-clause BSD"
__maintainer__ = "Ian Goodfellow"


""" An implementation of the PDDBM based on unrolling the inference
    loop into a huge theano graph.
"""


import time
from pylearn2.expr.nnet import sigmoid_numpy
from pylearn2.models.model import Model
from theano import config, function
import theano.tensor as T
import numpy as np
import warnings
from theano.gof.op import get_debug_values, debug_error_message
from pylearn2.utils import sharedX, as_floatX
import theano
import gc

warnings.warn('pddbm changing the recursion limit')
import sys
sys.setrecursionlimit(50000)

from pylearn2.models.s3c import full_min
from pylearn2.models.s3c import full_max
from pylearn2.models.s3c import reflection_clip
from pylearn2.models.s3c import damp
from pylearn2.models.s3c import S3C
from pylearn2.models.s3c import SufficientStatistics
from theano.printing import min_informative_str
from theano.printing import Print
from theano.gof.op import debug_assert
from theano.gof.op import get_debug_values
from pylearn2.space import VectorSpace
from theano.sandbox.scan import scan

warnings.warn('There is a known bug where for some reason the w field of s3c '
'gets serialized. Not sure if other things get serialized too but be sure to '
'call make_pseudoparams on everything you unpickle. I wish theano was not '
'such a piece of shit!')

def flatten(collection):
    rval = set([])

    for elem in collection:
        if hasattr(elem,'__len__'):
            rval = rval.union(flatten(elem))
        else:
            rval = rval.union([elem])

    return rval


class PDDBM(Model):

    """ Implements a model of the form
        P(v,s,h,g[0],...,g[N_g]) = S3C(v,s|h)DBM(h,g)
    """

    def __init__(self,
            s3c,
            dbm,
            bayes_B = False,
            ss_init_h = None,
            ss_init_scale = None,
            ss_init_mu = None,
            exhaustive_iteration = False,
            s3c_mu_learning_rate_scale = 1.,
            monitor_ranges = False,
            use_diagonal_natural_gradient = False,
            inference_procedure = None,
            learning_rate = 1e-3,
            init_non_s3c_lr = 1e-3,
            non_s3c_lr_start = 0,
            final_non_s3c_lr = 1e-3,
            min_shrink = .05,
            shrink_start = 0,
            lr_shrink_example_scale = 0.,
            non_s3c_lr_saturation_example = None,
            h_penalty = 0.0,
            s3c_l1_weight_decay = 0.0,
            h_target = None,
            g_penalties = None,
            g_targets = None,
            print_interval = 10000,
            freeze_s3c_params = False,
            freeze_dbm_params = False,
            dbm_weight_decay = None,
            dbm_l1_weight_decay = None,
            recons_penalty = None,
            sub_batch = False,
            init_momentum = None,
            final_momentum = None,
            momentum_saturation_example = None,
            h_bias_src = 'dbm',
            monitor_neg_chain_marginals = False):
        """
            s3c: a galatea.s3c.s3c.S3C object
                will become owned by the PDDBM
                it won't be deleted but many of its fields will change
            dbm: a pylearn2.dbm.DBM object
                will become owned by the PDDBM
                it won't be deleted but many of its fields will change
            inference_procedure: a galatea.pddbm.pddbm.InferenceProcedure
                                    if None, does not compile a learn_func
            print_interval: number of examples between each status printout
            h_bias_src: either 'dbm' or 's3c'. both the dbm and s3c have a bias
                    term on h-- whose should we use when we build the model?
                        if you want to start training with a new rbm on top
                        of an s3c model, it probably makes sense to make the
                        rbm have very small weights and take the biases from
                        s3c.
                      if you've already pretrained the rbm, then since this
                      model is basically P(g,h)P(v,s|h) it might make sense
                      to take the biases from the RBM
            freeze_dbm_params: If True, do not update parameters that are owned
                    exclusively by the dbm. (i.e., s3c.bias_hid will still be
                    updated, unless you also freeze_s3c_params)
            freeze_s3c_params: If True, do not update parameters that are owned
                    exclusively by s3c. (i.e., dbm.bias_vis will still be updated, unless you also freeze_dbm_params)
        """

        super(PDDBM,self).__init__()

        self.bayes_B = bayes_B

        self.exhaustive_iteration = exhaustive_iteration
        if self.exhaustive_iteration:
            self.iterator = None

        self.ss_init_h = ss_init_h
        self.ss_init_mu = ss_init_mu
        self.ss_init_scale = ss_init_scale

        self.min_shrink = np.cast['float32'](float(min_shrink))
        self.lr_shrink_example_scale = np.cast['float32'](float(lr_shrink_example_scale))
        self.shrink_start = shrink_start

        self.monitor_ranges = monitor_ranges

        self.learning_rate = learning_rate

        use_cd = dbm.use_cd
        self.use_cd = use_cd

        self.monitor_neg_chain_marginals = monitor_neg_chain_marginals

        self.dbm_weight_decay = dbm_weight_decay
        self.dbm_l1_weight_decay = dbm_l1_weight_decay
        self.use_diagonal_natural_gradient = use_diagonal_natural_gradient

        self.s3c_mu_learning_rate_scale = s3c_mu_learning_rate_scale

        self.init_non_s3c_lr = init_non_s3c_lr
        self.final_non_s3c_lr = final_non_s3c_lr
        self.non_s3c_lr_start = non_s3c_lr_start
        self.non_s3c_lr_saturation_example = non_s3c_lr_saturation_example

        self.init_momentum =   init_momentum
        self.final_momentum = final_momentum
        self.momentum_saturation_example = momentum_saturation_example

        self.recons_penalty = recons_penalty

        self.s3c = s3c
        s3c.e_step.autonomous = False

        if self.s3c.m_step is not None:
            m_step = self.s3c.m_step
            self.B_learning_rate_scale = m_step.B_learning_rate_scale
            self.alpha_learning_rate_scale = m_step.alpha_learning_rate_scale
            self.s3c_W_learning_rate_scale = m_step.W_learning_rate_scale
            if m_step.p_penalty is not None and m_step.p_penalty != 0.0:
                raise ValueError("s3c.p_penalty must be none or 0. p is not tractable anymore "
                        "when s3c is integrated into a pd-dbm.")
            self.B_penalty = m_step.B_penalty
            self.alpha_penalty = m_step.alpha_penalty
        else:
            self.B_learning_rate_scale = 1.
            self.alpha_learning_rate_scale = 1.
            self.s3c_W_learning_rate_scale = 1.
            self.B_penalty = 0.
            self.alpha_penalty = 0.

        s3c.m_step = None
        self.dbm = dbm
        self.dbm.use_cd = use_cd

        self.rng = np.random.RandomState([1,2,3])

        W = dbm.W[0].get_value()

        if ss_init_h is not None:
            W *= 0.
            W += (self.rng.uniform(0.,1., W.shape) < ss_init_h) * (ss_init_mu + self.rng.randn( * W.shape ) * ss_init_scale)
            dbm.W[0].set_value(W)


        if dbm.bias_vis.get_value(borrow=True).shape \
                != s3c.bias_hid.get_value(borrow=True).shape:
                    raise AssertionError("DBM has "+str(dbm.bias_vis.get_value(borrow=True).shape)+\
                            " visible units but S3C has "+str(s3c.bias_hid.get_value(borrow=True).shape))
        if h_bias_src == 'dbm':
            self.s3c.bias_hid = self.dbm.bias_vis
        elif h_bias_src == 's3c':
            self.dbm.bias_vis = self.s3c.bias_hid
        else:
            assert False

        self.nvis = s3c.nvis
        self.input_space = VectorSpace(self.nvis)

        self.freeze_s3c_params = freeze_s3c_params
        self.freeze_dbm_params = freeze_dbm_params

        #don't support some exotic options on s3c
        for option in ['monitor_functional',
                       'recycle_q',
                       'debug_m_step']:
            if getattr(s3c, option):
                warnings.warn('PDDBM does not support '+option+' in '
                        'the S3C layer, disabling it')
                setattr(s3c, option, False)

        s3c.monitor_stats = []
        s3c.e_step.monitor_stats = []

        if s3c.e_step.monitor_kl:
            s3c.e_step.monitor_kl = False

        self.print_interval = print_interval

        s3c.print_interval = None
        dbm.print_interval = None


        if inference_procedure is not None:
            inference_procedure.register_model(self)
        self.inference_procedure = inference_procedure

        self.num_g = len(self.dbm.W)

        self.h_penalty = h_penalty
        self.h_target = h_target

        self.g_penalties = g_penalties
        self.g_targets = g_targets

        self.s3c_l1_weight_decay = s3c_l1_weight_decay

        self.sub_batch = sub_batch

        self.redo_everything()



    def infer(self, V, Y = None, return_history = False):
        return self.inference_procedure.infer(V,Y,return_history)

    def get_weights(self):
        x = input('which weights you want?')

        if x == 2:
            """
            W1 = self.s3c.W.get_value()
            mu = self.s3c.mu.get_value()
            W1s = W1 * mu
            W2 = self.dbm.W[0].get_value()

            H = sigmoid_numpy( W2.T + self.dbm.bias_vis.get_value())

            rval = np.dot(H, W1s.T)
            assert rval.shape[0] == self.s3c.nhid
            assert rval.shape[1] == self.s3c.nvis

            return rval.T
            """

            return np.dot(self.s3c.W.get_value() \
                    * self.s3c.mu.get_value(), self.dbm.W[0].get_value())

        if x == 1:
            return self.s3c.get_weights()

        assert False

    def redo_everything(self):

        #we don't call redo_everything on s3c because this would reset its weights
        #however, calling redo_everything on the dbm just resets its negative chain
        self.dbm.redo_everything()

        if self.sub_batch:
             self.grads = {}
             for param in self.get_params():
                 self.grads[param] = sharedX(np.zeros(param.get_value().shape))


        if self.use_diagonal_natural_gradient:
            self.params_to_means = {}
            self.params_to_M2s = {}
            self.n_var_samples = sharedX(0.0)

            for param in self.get_params():
                self.params_to_means[param] = \
                        sharedX(np.zeros(param.get_value().shape))
                self.params_to_M2s[param] = \
                        sharedX(np.zeros(param.get_value().shape))

        if self.momentum_saturation_example is not None:
            assert not self.use_diagonal_natural_gradient
            self.params_to_incs = {}

            for param in self.get_params():
                self.params_to_incs[param] = sharedX(np.zeros(param.get_value().shape), name = param.name+'_inc')

            self.momentum = sharedX(self.init_momentum, name='momentum')

        if self.non_s3c_lr_saturation_example is not None:
            self.non_s3c_lr = sharedX(self.init_non_s3c_lr, name = 'non_s3c_lr')

        self.test_batch_size = 2

        self.redo_theano()


    def set_dtype(self, dtype):

        assert self.s3c.bias_hid is self.dbm.bias_vis
        super(PDDBM, self).set_dtype(dtype)
        self.s3c.bias_hid = self.dbm.bias_vis

    def get_monitoring_channels(self, V, Y = None):

        if Y is None:
            assert self.dbm.num_classes == 0
        if self.dbm.num_classes == 0:
            Y = None

        try:
            self.compile_mode()

            self.s3c.set_monitoring_channel_prefix('s3c_')

            rval = self.s3c.get_monitoring_channels(V)

            # % of DBM W[0] weights that are negative
            negs = T.sum(T.cast(T.lt(self.dbm.W[0],0.),'float32'))
            total = np.cast['float32'](self.dbm.rbms[0].nvis * self.dbm.rbms[0].nhid)
            negprop = negs /total
            rval['dbm_W[0]_negprop'] = negprop


            rval['shrink'] = self.shrink

            if self.monitor_neg_chain_marginals:
                assert self.dbm.negative_chains > 0
                V_mean_samples = self.s3c.random_design_matrix(batch_size = self.dbm.negative_chains,
                        H_sample = self.dbm.V_chains,
                        S_sample = self.s3c.mu.dimshuffle('x',0), full_sample = False)
                V_mean = V_mean_samples.mean(axis=0)
                rval['marginal_V_mean_min'] = V_mean.min()
                rval['marginal_V_mean_mean'] = V_mean.mean()
                rval['marginal_V_mean_max'] = V_mean.max()



            #DBM negative chain
            H_chain = self.dbm.V_chains.mean(axis=0)
            rval['neg_chain_h_min'] = full_min(H_chain)
            rval['neg_chain_h_mean'] = H_chain.mean()
            rval['neg_chain_h_max'] = full_max(H_chain)
            if self.dbm.Y_chains is not None:
                Y_chain = self.dbm.Y_chains.mean(axis=0)
                rval['neg_chain_y_min'] = full_min(Y_chain)
                rval['neg_chain_y_mean'] = Y_chain.mean()
                rval['neg_chain_y_max'] = full_max(Y_chain)
            G_chains = self.dbm.H_chains
            for i, G_chain in enumerate(G_chains):
                G_chain = G_chain.mean(axis=0)
                rval['neg_chain_g[%d]_min'%i] = full_min(G_chain)
                rval['neg_chain_g[%d]_mean'%i] = G_chain.mean()
                rval['neg_chain_g[%d]_max'%i] = full_max(G_chain)
            rb, rby = self.dbm.rao_blackwellize( self.dbm.V_chains, G_chains, self.dbm.Y_chains)
            for i, rbg in enumerate(rb):
                rbg = rbg.mean(axis=0)
                rval['neg_chain_rbg[%d]_min'%i] = full_min(rbg)
                rval['neg_chain_rbg[%d]_mean'%i] = rbg.mean()
                rval['neg_chain_rbg[%d]_max'%i] = full_max(rbg)
            if rby is not None:
                rby = rby.mean(axis=0)
                rval['neg_chain_rby_min'] = full_min(rby)
                rval['neg_chain_rby_mean'] = rby.mean()
                rval['neg_chain_rby_max'] = full_max(rby)

            from_inference_procedure = self.inference_procedure.get_monitoring_channels(V,Y)

            rval.update(from_inference_procedure)


            self.dbm.set_monitoring_channel_prefix('dbm_')

            from_dbm = self.dbm.get_monitoring_channels(V)

            #remove the s3c bias_hid channels from the DBM's output
            keys_to_del = []
            for key in from_dbm:
                if key.startswith('dbm_bias_hid_'):
                    keys_to_del.append(key)
            for key in keys_to_del:
                del from_dbm[key]

            rval.update(from_dbm)

            if self.use_diagonal_natural_gradient:
                for param in self.get_params():
                    name = 'grad_var_'+param.name

                    params_to_variances = self.get_params_to_variances()

                    var = params_to_variances[param]

                    rval[name+'_min'] = var.min()
                    rval[name+'_mean'] = var.mean()
                    rval[name+'_max'] = var.max()

            if self.momentum_saturation_example is not None:
                rval['momentum'] = self.momentum
            if self.non_s3c_lr_saturation_example is not None:
                rval['non_s3c_lr'] = self.non_s3c_lr

        finally:
            self.deploy_mode()

        return rval

    def get_output_space(self):
        return self.dbm.get_output_space()

    def compile_mode(self):
        """ If any shared variables need to have batch-size dependent sizes,
        sets them all to the sizes used for interactive debugging during graph construction """
        pass

    def deploy_mode(self):
        """ If any shared variables need to have batch-size dependent sizes, sets them all to their runtime sizes """
        pass

    def get_params(self):

        params = set([])

        if not self.freeze_s3c_params:
            params = params.union(set(self.s3c.get_params()))

        if not self.freeze_dbm_params:
            params = params.union(set(self.dbm.get_params()))
        else:
            assert False #temporary debugging assert

        assert self.dbm.bias_hid[0] in params

        return list(params)

    def make_reset_grad_func(self):
        """
        For use with the sub_batch feature only
        Resets the gradient to the data-independent gradient (ie, negative phase, regularization)
        One can then accumulate the positive phase gradient in sub-batches
        """

        assert self.sub_batch

        assert self.g_penalties is None
        assert self.h_penalty == 0.0
        assert self.dbm_weight_decay is None
        assert self.dbm_l1_weight_decay is None
        assert not self.use_cd

        params_to_approx_grads = self.dbm.get_neg_phase_grads()

        updates = {}

        for param in self.grads:
            if param in params_to_approx_grads:
                updates[self.grads[param]] = params_to_approx_grads[param]
            else:
                updates[self.grads[param]] = T.zeros_like(param)

        sampling_updates = self.dbm.get_sampling_updates()

        for key in sampling_updates:
            assert key not in updates
            updates[key] = sampling_updates[key]

        f = function([], updates = updates)

        return f



    def make_accum_pos_phase_grad_func(self, V):

        hidden_obs = self.inference_procedure.infer(V)

        obj, constants = self.positive_phase_obj(V, hidden_obs)

        updates = {}

        for param in self.grads:
            updates[self.grads[param]] = self.grads[param] + T.grad(obj, param, \
                    consider_constant = constants)

        f = function([V], updates= updates)

        return f

    def positive_phase_obj(self, V, hidden_obs):
        """ returns both the objective AND things that should be considered constant
            in order to avoid propagating through inference """

        #make a restricted dictionary containing only vars s3c knows about
        restricted_obs = {}
        for key in hidden_obs:
            if key != 'G_hat':
                restricted_obs[key] = hidden_obs[key]


        #request s3c sufficient statistics
        needed_stats = \
         S3C.expected_log_prob_v_given_hs_needed_stats().union(\
         S3C.expected_log_prob_s_given_h_needed_stats())
        stats = SufficientStatistics.from_observations(needed_stats = needed_stats,
                V = V, **restricted_obs)

        #don't backpropagate through inference
        obs_set = set(hidden_obs.values())
        stats_set = set(stats.d.values())
        constants = flatten(obs_set.union(stats_set))

        G_hat = hidden_obs['G_hat']
        for i, G in enumerate(G_hat):
            G.name = 'final_G_hat[%d]' % (i,)
        H_hat = hidden_obs['H_hat']
        H_hat.name = 'final_H_hat'
        S_hat = hidden_obs['S_hat']
        S_hat.name = 'final_S_hat'

        expected_log_prob_v_given_hs = self.s3c.expected_log_prob_v_given_hs(stats, \
                H_hat = H_hat, S_hat = S_hat)
        assert len(expected_log_prob_v_given_hs.type.broadcastable) == 0


        expected_log_prob_s_given_h  = self.s3c.expected_log_prob_s_given_h(stats)
        assert len(expected_log_prob_s_given_h.type.broadcastable) == 0


        expected_dbm_energy = self.dbm.expected_energy( V_hat = H_hat, H_hat = G_hat )
        assert len(expected_dbm_energy.type.broadcastable) == 0

        #note: this is not the complete tractable part of the objective
        #the objective also includes the entropy of Q, but we drop that since it is
        #not a function of the parameters and we're not able to compute the true
        #value of the objective function anyway
        obj = expected_log_prob_v_given_hs + \
                        expected_log_prob_s_given_h  - \
                        expected_dbm_energy

        assert len(obj.type.broadcastable) == 0

        return obj, constants

    def make_grad_step_func(self):

        learning_updates = self.get_param_updates(self.grads)
        self.censor_updates(learning_updates)



        print "compiling function..."
        t1 = time.time()
        rval = function([], updates = learning_updates)
        t2 = time.time()
        print "... compilation took "+str(t2-t1)+" seconds"

        return rval



    def make_learn_func(self, V, Y):
        """
        V: a symbolic design matrix
        Y: None or a symbolic label matrix, one label per row, one-hot encoding
        """

        assert self.inference_procedure is not None

        #run variational inference on the train set
        hidden_obs = self.inference_procedure.infer(V,Y)

        #make a restricted dictionary containing only vars s3c knows about
        restricted_obs = {}
        for key in hidden_obs:
            if key != 'G_hat':
                restricted_obs[key] = hidden_obs[key]


        #request s3c sufficient statistics
        needed_stats = \
         S3C.expected_log_prob_v_given_hs_needed_stats().union(\
         S3C.expected_log_prob_s_given_h_needed_stats())
        stats = SufficientStatistics.from_observations(needed_stats = needed_stats,
                V = V, **restricted_obs)

        #don't backpropagate through inference
        obs_set = set(hidden_obs.values())
        stats_set = set(stats.d.values())
        constants = flatten(obs_set.union(stats_set))

        G_hat = hidden_obs['G_hat']
        for i, G in enumerate(G_hat):
            G.name = 'final_G_hat[%d]' % (i,)
        H_hat = hidden_obs['H_hat']
        H_hat.name = 'final_H_hat'
        S_hat = hidden_obs['S_hat']
        S_hat.name = 'final_S_hat'

        assert H_hat in constants
        for G in G_hat:
            assert G in constants
        assert S_hat in constants

        expected_log_prob_v_given_hs = self.s3c.expected_log_prob_v_given_hs(stats, \
                H_hat = H_hat, S_hat = S_hat)
        assert len(expected_log_prob_v_given_hs.type.broadcastable) == 0


        expected_log_prob_s_given_h  = self.s3c.expected_log_prob_s_given_h(stats)
        assert len(expected_log_prob_s_given_h.type.broadcastable) == 0


        expected_dbm_energy = self.dbm.expected_energy( V_hat = H_hat, H_hat = G_hat, Y_hat = Y )
        assert len(expected_dbm_energy.type.broadcastable) == 0

        test = T.grad(expected_dbm_energy, self.dbm.W[0], consider_constant = constants)

        #note: this is not the complete tractable part of the objective
        #the objective also includes the entropy of Q, but we drop that since it is
        #not a function of the parameters and we're not able to compute the true
        #value of the objective function anyway
        tractable_obj = expected_log_prob_v_given_hs + \
                        expected_log_prob_s_given_h  - \
                        expected_dbm_energy

        assert len(tractable_obj.type.broadcastable) == 0


        if self.dbm_weight_decay:

            for i, t in enumerate(zip(self.dbm_weight_decay, self.dbm.W)):

                coeff, W = t

                coeff = as_floatX(float(coeff))
                coeff = T.as_tensor_variable(coeff)
                coeff.name = 'dbm_weight_decay_coeff_'+str(i)

                tractable_obj = tractable_obj - coeff * T.mean(T.sqr(W))

        if self.dbm_l1_weight_decay:

            for i, t in enumerate(zip(self.dbm_l1_weight_decay, self.dbm.W)):

                coeff, W = t

                coeff = as_floatX(float(coeff))
                coeff = T.as_tensor_variable(coeff)
                coeff.name = 'dbm_l1_weight_decay_coeff_'+str(i)

                tractable_obj = tractable_obj - coeff * T.sum(abs(W))

        if self.h_penalty != 0.0:
            next_h = self.inference_procedure.infer_H_hat(V = V,
                H_hat = H_hat, S_hat = S_hat, G1_hat = G_hat[0])

            #err = next_h.mean(axis=0) - self.h_target

            #abs_err = abs(err)

            penalty = T.sum( T.nnet.binary_crossentropy( target = self.h_target,
                output = next_h.mean(axis=0)) )

            tractable_obj =  tractable_obj - self.h_penalty * penalty

        if self.g_penalties is not None:
            for i in xrange(len(self.dbm.bias_hid)):
                G = self.inference_procedure.infer_G_hat(H_hat = H_hat, G_hat = G_hat, idx = i)

                g = T.mean(G,axis=0)

                #err = g - self.g_targets[i]

                #abs_err = abs(err)

                #penalty = T.mean(abs_err)

                penalty = T.sum( T.nnet.binary_crossentropy( target = self.g_targets[i], output = g))

                tractable_obj = tractable_obj - self.g_penalties[i] * penalty

        if self.s3c_l1_weight_decay != 0.0:

            tractable_obj = tractable_obj - self.s3c_l1_weight_decay * T.mean(abs(self.s3c.W))

        if self.B_penalty != 0.0:
            tractable_obj = tractable_obj - T.mean(self.s3c.B) * self.B_penalty

        if self.alpha_penalty != 0.0:
            tractable_obj = tractable_obj - T.mean(self.s3c.alpha) * self.alpha_penalty


        if self.recons_penalty is not None:
            tractable_obj = tractable_obj - self.recons_penalty * self.simple_recons_error(V, G_hat)

        assert len(tractable_obj.type.broadcastable) == 0
        assert tractable_obj.type.dtype == config.floatX

        #take the gradient of the tractable part
        params = self.get_params()
        grads = T.grad(tractable_obj, params, consider_constant = constants, disconnected_inputs = 'warn')

        #put gradients into convenient dictionary
        params_to_grads = {}
        for param, grad in zip(params, grads):
            params_to_grads[param] = grad

        #make function for online estimate of variance of grad
        #this is kind of a hack, since I install the function rather
        #than returning it. should clean this up

        if self.use_diagonal_natural_gradient:
            new_n = self.n_var_samples + as_floatX(1.)

            var_updates = { self.n_var_samples : new_n }

            for param in params:
                grad = params_to_grads[param]
                mean = self.params_to_means[param]
                M2 = self.params_to_M2s[param]

                delta = grad - mean
                new_mean = mean + delta / new_n

                var_updates[M2] = M2 + delta * (grad - new_mean)
                var_updates[mean] = new_mean

            self.update_variances = function([V], updates = var_updates)

        #end hacky part


        #add the approximate gradients
        if self.use_cd:
            params_to_approx_grads = self.dbm.get_cd_neg_phase_grads(V = H_hat, H_hat = G_hat, Y = Y)
        else:
            params_to_approx_grads = self.dbm.get_neg_phase_grads()

        for param in params_to_approx_grads:
            if param in params_to_grads:
                params_to_grads[param] = params_to_grads[param] + params_to_approx_grads[param]
                params_to_grads[param].name = param.name + '_final_approx_grad'

        if self.use_diagonal_natural_gradient:

            params_to_variances = self.get_params_to_variances()

            for param in set(self.dbm.W).union(self.dbm.bias_hid):

                grad = params_to_grads[param]
                var = params_to_variances[param]
                safe_var = var + as_floatX(.5)
                scaled_grad = grad / safe_var
                params_to_grads[param] = scaled_grad

        assert self.dbm.bias_hid[0] in params_to_grads
        learning_updates = self.get_param_updates(params_to_grads)
        assert self.dbm.bias_hid[0] in learning_updates

        if self.use_cd:
            sampling_updates = {}
        else:
            sampling_updates = self.dbm.get_sampling_updates()

        for key in sampling_updates:
            learning_updates[key] = sampling_updates[key]

        self.censor_updates(learning_updates)


        #print 'learning updates contains: '
        #for key in learning_updates:
        #    print '\t',key
        #print min_informative_str(learning_updates[self.dbm.bias_hid[0]])
        #assert False

        inputs = [ V ]

        if Y is not None:
            inputs.append(Y)

        print "compiling PD-DBM learn function..."
        t1 = time.time()
        rval = function(inputs, updates = learning_updates)
        t2 = time.time()
        print "... compilation took "+str(t2-t1)+" seconds"
        print "graph size: ",len(rval.maker.env.toposort())

        return rval


    def simple_recons_error(self, V, G_hat):
        """
            makes a single downward meanfield pass from the deepest layer
            to estimate a reconstruction
            returns the mean squared error of that reconstruction
            NOTE: alpha has no effect on this. we might want to do
            E[ error(V,recons) ] rather than error(V,E[recons]) so
            that alpha gets encouraged to be small
        """

        assert len(G_hat) == 1
        H = T.nnet.sigmoid(
                T.dot(G_hat[0], self.dbm.W[0].T) + self.dbm.bias_vis)
        HS = H * self.s3c.mu
        recons = T.dot(HS, self.s3c.W.T)

        return T.mean(T.sqr(recons - V))

    def get_param_updates(self, params_to_grads):

        warnings.warn("TODO: get_param_updates does not use geodesics for now")

        rval = {}

        learning_rate = {}

        for param in params_to_grads:
            if param is self.s3c.B_driver:
                learning_rate[param] = as_floatX(self.learning_rate * self.B_learning_rate_scale)
            elif param is self.s3c.alpha:
                learning_rate[param] = as_floatX(self.learning_rate * self.alpha_learning_rate_scale)
            elif param is self.s3c.W:
                learning_rate[param] = as_floatX(self.learning_rate * self.s3c_W_learning_rate_scale)
            elif param is self.s3c.mu:
                learning_rate[param] = as_floatX(self.learning_rate * self.s3c_mu_learning_rate_scale)
            elif param not in self.s3c.get_params():
                if self.non_s3c_lr_saturation_example is not None:
                    learning_rate[param] = self.non_s3c_lr
                else:
                    learning_rate[param] = as_floatX(self.learning_rate)
            else:
                learning_rate[param] = as_floatX(self.learning_rate)

        self.shrink = sharedX(1.0)

        for param in learning_rate:
            learning_rate[param] = self.shrink * learning_rate[param]

        if self.momentum_saturation_example is not None:
            for key in params_to_grads:
                inc = self.params_to_incs[key]
                rval[inc] = self.momentum * inc + learning_rate[key] * params_to_grads[key]
                rval[key] = key + rval[inc]
        else:
            for key in params_to_grads:
                rval[key] = key + learning_rate[key] * params_to_grads[key]


        for param in self.get_params():
            assert param in params_to_grads
            assert param in rval

        assert self.dbm.bias_hid[0] in rval

        return rval

    def censor_updates(self, updates):

        if self.freeze_s3c_params:
            for param in self.s3c.get_params():
                assert param not in updates or param is self.dbm.bias_vis

        if self.freeze_dbm_params:
            for param in self.dbm.get_params():
                if param in updates and param is not self.s3c.bias_hid:
                    assert hasattr(param,'name')
                    name = 'anon'
                    if param.name is not None:
                        name = param.name
                    raise AssertionError("DBM parameters are frozen but you're trying to update DBM parameter "+name)

        self.s3c.censor_updates(updates)
        self.dbm.censor_updates(updates)

    def random_design_matrix(self, batch_size, theano_rng):

        if not hasattr(self,'p'):
            self.make_pseudoparams()

        H_sample = self.dbm.random_design_matrix(batch_size, theano_rng)

        V_sample = self.s3c.random_design_matrix(batch_size, theano_rng, H_sample = H_sample)

        return V_sample


    def make_pseudoparams(self):
        self.s3c.make_pseudoparams()


    def get_params_to_variances(self):

        rval = {}

        for param in self.get_params():
            M2 = self.params_to_M2s[param]
            rval[param] = M2 / (self.n_var_samples - as_floatX(1.))

        return rval

    def redo_theano(self):

        self.s3c.reset_censorship_cache()

        try:
            self.compile_mode()

            init_names = dir(self)

            self.make_pseudoparams()

            X = T.matrix(name='V')
            X.tag.test_value = np.cast[config.floatX](self.rng.randn(self.test_batch_size,self.nvis))

            if self.dbm.num_classes > 0:
                Y = T.matrix(name='Y')
            else:
                Y = None

            if self.use_diagonal_natural_gradient:
                updates = { self.n_var_samples : as_floatX(0.0) }

                for param in self.get_params():
                    updates[self.params_to_means[param]] = \
                            0. * self.params_to_means[param]
                    updates[self.params_to_M2s[param]] = \
                            0. * self.params_to_M2s[param]
                self.reset_variances = function([],updates= updates)

            self.s3c.e_step.register_model(self.s3c)

            if self.sub_batch:
                if Y is not None:
                    raise NotImplementedError("sub_batch mode does not support labels yet")
                self.reset_grad_func = self.make_reset_grad_func()
                self.accum_pos_phase_grad_func = self.make_accum_pos_phase_grad_func(X)
                self.grad_step_func = self.make_grad_step_func()
            else:
                if self.inference_procedure is not None:
                    self.learn_func = self.make_learn_func(X,Y)

            final_names = dir(self)

            self.register_names_to_del([name for name in final_names if name not in init_names])
        finally:
            self.deploy_mode()
        #end try block
    #end redo_theano

    def learn(self, dataset, batch_size):


        if self.bayes_B:
            self.bayes_B = False

            var = dataset.X.var(axis=0)

            assert not self.s3c.tied_B
            self.s3c.B_driver.set_value( 1. / (var + .01) )

        if self.exhaustive_iteration:
            def make_iterator():
                self.iterator = dataset.iterator(
                        mode = 'sequential',
                        batch_size = batch_size,
                        targets = self.dbm.num_classes > 0)

            if self.iterator is None:
                self.batch_size = batch_size
                self.dataset = dataset
                self.register_names_to_del(['dataset','iterator'])
                make_iterator()
            else:
                assert dataset is self.dataset
                assert batch_size == self.batch_size
            if self.dbm.num_classes > 0:
                try:
                    X, Y = self.iterator.next()
                except StopIteration:
                    print 'Finished a dataset-epoch'
                    make_iterator()
                    X, Y = self.iterator.next()
            else:
                Y = None
                try:
                    X = self.iterator.next()
                except StopIteration:
                    print 'Finished a dataset-epoch'
                    make_iterator()
                    X = self.iterator.next()
        else:
            if self.dbm.num_classes > 0:
                raise NotImplementedError("Random iteration doesn't support using class labels yet")
            X = dataset.get_batch_design(batch_size)
            Y = None

        self.learn_mini_batch(X,Y)

    def learn_mini_batch(self, X, Y = None):

        assert False #bring down the job so the profiler output gets written out

        assert (Y is None) == (self.dbm.num_classes == 0)

        self.shrink.set_value( np.cast['float32']( \
                max(self.min_shrink,
                    1. / (1. + self.lr_shrink_example_scale * float( \
                            max(0,self.monitor.get_examples_seen() - float(self.shrink_start)))))))

        assert self.s3c is self.inference_procedure.s3c_e_step.model
        if self.momentum_saturation_example is not None:
            alpha = float(self.monitor.get_examples_seen()) / float(self.momentum_saturation_example)
            alpha = min( alpha, 1.0)
            self.momentum.set_value(np.cast[config.floatX](
                (1.-alpha) * self.init_momentum + alpha * self.final_momentum))
        if self.non_s3c_lr_saturation_example is not None:
            alpha = (float(self.monitor.get_examples_seen()) - float(self.non_s3c_lr_start)) / (float(self.non_s3c_lr_saturation_example) - float(self.non_s3c_lr_start))
            alpha = max( alpha, 0.0)
            alpha = min( alpha, 1.0)
            self.non_s3c_lr.set_value(np.cast[config.floatX](
                (1.-alpha) * self.init_non_s3c_lr + alpha * self.final_non_s3c_lr))



        if self.use_diagonal_natural_gradient:
            if self.dbm.num_classes > 0:
                raise NotImplementedError()
            self.reset_variances()
            for i in xrange(X.shape[0]):
                self.update_variances(X[i:i+1,:])

        if self.sub_batch:
            self.reset_grad_func()
            for i in xrange(X.shape[0]):
                self.accum_pos_phase_grad_func(X[i:i+1,:])
        else:
            if Y is None:
                self.learn_func(X)
            else:
                self.learn_func(X,Y)
        if self.monitor._examples_seen % self.print_interval == 0:
            print ""
            print "S3C:"
            self.s3c.print_status()
            print "DBM:"
            self.dbm.print_status()

    def get_weights_format(self):
        return self.s3c.get_weights_format()

class InferenceProcedure:
    """

    Variational inference

    """

    def __init__(self, schedule,
                       clip_reflections = False,
                       monitor_kl = False,
                       rho = 0.5):
        """Parameters
        --------------
        schedule:
            list of steps. each step can consist of one of the following:
                ['s', <new_coeff>] where <new_coeff> is a number between 0. and 1.
                    does a damped parallel update of s, putting <new_coeff> on the new value of s
                ['h', <new_coeff>] where <new_coeff> is a number between 0. and 1.
                    does a damped parallel update of h, putting <new_coeff> on the new value of h
                ['g', idx]
                    does a block update of g[idx]

        clip_reflections, rho : if clip_reflections is true, the update to Mu1[i,j] is
            bounded on one side by - rho * Mu1[i,j] and unbounded on the other side
        """

        self.schedule = schedule

        self.clip_reflections = clip_reflections
        self.monitor_kl = monitor_kl

        self.rho = as_floatX(rho)

        self.model = None



    def final_diff(self, history, letter, layer = None):
        """
            history: the output of infer with return_history = True
            letter: 's', 'h', or 'g'. whether to look at S_hat, H_hat, or G_hat
            layer: if letter == 'g', the layer within G_hat

            returns a symbolic expression for
                the final value of a variational parameter minus
                the second-to-last value it took during optimization
        """


        ultimate_update = len(history) - 1

        def get_var(d):
            if letter == 'h':
                return d['H_hat']
            elif letter == 's':
                return d['S_hat']
            elif letter == 'g':
                return d['G_hat'][layer]
            raise ValueError("Bad letter code: "+str(letter))

        while True:
            penultimate_update = ultimate_update - 1
            ultimate_var = get_var(history[ultimate_update])
            penultimate_var = get_var(history[penultimate_update])
            if not (ultimate_var is penultimate_var):
                return ultimate_var - penultimate_var
            ultimate_update -= 1

    def get_monitoring_channels(self, V, Y = None):

        assert (Y is None) == (self.model.dbm.num_classes == 0)

        rval = {}

        obs_history = self.infer(V, Y, return_history = True)

        rval["final_diff_S_hat"] = abs(self.final_diff(obs_history, 's')).max()
        rval["final_diff_H_hat"] = abs(self.final_diff(obs_history, 'h')).max()
        for i in xrange(len(self.model.dbm.rbms)):
            rval["final_diff_G_hat[%d]"%i] = abs(self.final_diff(obs_history, 'g', i)).max()

        if self.monitor_kl not in [False, 0]:
            assert self.monitor_kl == True or isinstance(self.monitor_kl, list)

            if isinstance(self.monitor_kl, list):
                steps = [ elem for elem in self.monitor_kl]
                for i in xrange(len(steps)):
                    assert steps[i] < 2 + len(self.schedule)
                    if steps[i] < 0:
                        steps[i] = len(self.schedule) + 2 + steps[i]
                    assert steps[i] > 0
            else:
                steps = xrange(1, 2 + len(self.schedule))

            for i in steps:
                obs = obs_history[i-1]

                if i == 1:
                    summary = '(init)'
                else:
                    step = self.schedule[i-2]
                    summary = str(step)

                for G_hat in obs['G_hat']:
                    for Gv in get_debug_values(G_hat):
                        assert Gv.min() >= 0.0
                        assert Gv.max() <= 1.0

                channel_val = self.truncated_KL(V, obs, Y).mean()
                assert channel_val.dtype == 'float32'
                rval['trunc_KL_'+str(i)+summary] = channel_val

        final_vals = obs_history[-1]

        S_hat = final_vals['S_hat']
        H_hat = final_vals['H_hat']
        h = T.mean(H_hat, axis=0)

        rval['h_min'] = full_min(h)
        rval['h_mean'] = T.mean(h)
        rval['h_max'] = full_max(h)

        Gs = final_vals['G_hat']

        for i, G in enumerate(Gs):

            g = T.mean(G,axis=0)

            rval['g[%d]_min'%(i,)] = full_min(g)
            rval['g[%d]_mean'%(i,)] = T.mean(g)
            rval['g[%d]_max'%(i,)] = full_max(g)


        #norm of gradient with respect to variational params
        grad_norm_sq = np.cast[config.floatX](0.)
        kl = self.truncated_KL(V, obs_history[-1], Y).mean()
        for var_param in set([ S_hat, H_hat]).union(Gs):
            grad = T.grad(kl,var_param)
            grad_norm_sq = grad_norm_sq + T.sum(T.sqr(grad))
        grad_norm = T.sqrt(grad_norm_sq)
        rval['var_param_grad_norm'] = grad_norm




        if self.model.monitor_ranges:
            S_hat = final_vals['S_hat']
            HS = H_hat * S_hat

            hs_max = T.max(HS,axis=0)
            hs_min = T.min(HS,axis=0)

            hs_range = hs_max - hs_min

            rval['hs_range_min'] = T.min(hs_range)
            rval['hs_range_mean'] = T.mean(hs_range)
            rval['hs_range_max'] = T.max(hs_range)

            h_max = T.max(H_hat,axis=0)
            h_min = T.min(H_hat,axis=0)

            h_range = h_max - h_min

            rval['h_range_min'] = T.min(h_range)
            rval['h_range_mean'] = T.mean(h_range)
            rval['h_range_max'] = T.max(h_range)

            for i, G in enumerate(Gs):

                g_max = T.max(G,axis=0)
                g_min = T.min(G,axis=0)

                g_range = g_max - g_min

                g_name = 'g[%d]' % (i,)

                rval[g_name+'_range_min'] = T.min(g_range)
                rval[g_name+'_range_mean'] = T.mean(g_range)
                rval[g_name+'_range_max'] = T.max(g_range)


        #if self.model.recons_penalty is not None:
        rval['simple_recons_error'] = self.model.simple_recons_error(V,Gs)



        return rval

    def register_model(self, model):
        self.model = model

        self.s3c_e_step = self.model.s3c.e_step

        self.s3c_e_step.clip_reflections = self.clip_reflections
        self.s3c_e_step.rho = self.rho

        self.dbm_ip = self.model.dbm.inference_procedure


    def dbm_observations(self, obs):

        rval = {}
        rval['H_hat'] = obs['G_hat']
        rval['V_hat'] = obs['H_hat']

        return rval

    def truncated_KL(self, V, obs, Y = None):
        """ KL divergence between variational and true posterior, dropping terms that don't
            depend on the variational parameters """

        for G_hat in obs['G_hat']:
            for Gv in get_debug_values(G_hat):
                assert Gv.min() >= 0.0
                assert Gv.max() <= 1.0

        s3c_truncated_KL = self.s3c_e_step.truncated_KL(V, Y = None, obs = obs)
        assert len(s3c_truncated_KL.type.broadcastable) == 1

        dbm_obs = self.dbm_observations(obs)

        dbm_truncated_KL = self.dbm_ip.truncated_KL(V = obs['H_hat'], Y = Y, obs = dbm_obs, no_v_bias = True)
        assert len(dbm_truncated_KL.type.broadcastable) == 1

        for s3c_kl_val, dbm_kl_val in get_debug_values(s3c_truncated_KL, dbm_truncated_KL):
            debug_assert( not np.any(np.isnan(s3c_kl_val)))
            debug_assert( not np.any(np.isnan(dbm_kl_val)))

        rval = s3c_truncated_KL + dbm_truncated_KL

        return rval

    def infer_H_hat(self, V, H_hat, S_hat, G1_hat):
        """
            G1_hat: variational parameters for the g layer closest to h
                    here we use the designation from the math rather than from
                    the list, where it is G_hat[0]
        """

        s3c_presigmoid = self.s3c_e_step.infer_H_hat_presigmoid(V, H_hat, S_hat)

        W = self.model.dbm.W[0]

        assert self.model.s3c.bias_hid is self.model.dbm.bias_vis

        top_down = T.dot(G1_hat, W.T)

        presigmoid = s3c_presigmoid + top_down

        H = T.nnet.sigmoid(presigmoid)

        return H


    def infer_G_hat(self, H_hat, G_hat, idx, Y_hat = None):

        assert (Y_hat is None) == (self.model.dbm.num_classes == 0)

        number = idx
        dbm_ip = self.model.dbm.inference_procedure

        b = self.model.dbm.bias_hid[number]

        W = self.model.dbm.W

        W_below = W[number]

        if number == 0:
            H_hat_below = H_hat
        else:
            H_hat_below = G_hat[number - 1]

        num_g = self.model.num_g

        if number == num_g - 1:
            if Y_hat is None:
                return dbm_ip.infer_H_hat_one_sided(other_H_hat = H_hat_below, W = W_below, b = b)
            else:
                for Y_hat_v in get_debug_values(Y_hat):
                    assert Y_hat_v.shape[1] == self.model.dbm.num_classes
                    assert self.model.dbm.num_classes == 10 #temporary debugging assert, can remove
                return dbm_ip.infer_H_hat_two_sided(H_hat_below = H_hat_below, W_below = W_below, b = b,
                        H_hat_above = Y_hat, W_above = self.model.dbm.W_class)
        else:
            H_hat_above = G_hat[number + 1]
            W_above = W[number+1]
            return dbm_ip.infer_H_hat_two_sided(H_hat_below = H_hat_below, H_hat_above = H_hat_above,
                                   W_below = W_below, W_above = W_above,
                                   b = b)

    def infer_var_s1_hat(self):
        return self.s3c_e_step.infer_var_s1_hat()


    def infer_var_s0_hat(self):
        return self.s3c_e_step.infer_var_s0_hat()

    def infer(self, V, Y = None, return_history = False):
        """

            return_history: if True:
                                returns a list of dictionaries with
                                showing the history of the variational
                                parameters
                                throughout fixed point updates
                            if False:
                                returns a dictionary containing the final
                                variational parameters
            Y:
                None corresponds to a model that has no labels
                -1 corresponds to inferring Y in a model that has labels
                    (each G_hat update will become a G_hat-Y_hat-G_hat update)
                a theano matrix corresponds to clamping Y to that matrix
        """

        assert Y not in [True,False,0,1] #detect bug where Y gets something that was meant to be return_history
        assert (Y is None) == (self.model.dbm.num_classes == 0)

        infer_labels = Y == -1


        alpha = self.model.s3c.alpha
        s3c_e_step = self.s3c_e_step
        dbm_ip = self.dbm_ip

        var_s0_hat = 1. / alpha
        var_s1_hat = s3c_e_step.infer_var_s1_hat()

        if infer_labels:
            Y = dbm_ip.init_Y_hat(V)

        H_hat = s3c_e_step.init_H_hat(V)
        G_hat = dbm_ip.init_H_hat(H_hat)
        S_hat = s3c_e_step.init_S_hat(V)

        H_hat.name = 'init_H_hat'
        S_hat.name = 'init_S_hat'

        for Hv in get_debug_values(H_hat):

            if Hv.shape[1] != s3c_e_step.model.nhid:
                debug_error_message('H prior has wrong # hu, expected %d actual %d'%\
                        (s3c_e_step.model.nhid,
                            Hv.shape[1]))

        for Sv in get_debug_values(S_hat):
            if isinstance(Sv, tuple):
                warnings.warn("I got a tuple from this and I have no idea why the fuck that happens. Pulling out the single element of the tuple")
                Sv ,= Sv

            if Sv.shape[1] != s3c_e_step.model.nhid:
                debug_error_message('prior has wrong # hu, expected %d actual %d'%\
                        (s3c_e_step.model.nhid,
                            Sv.shape[1]))

        def check_H(my_H, my_V):
            if my_H.dtype != config.floatX:
                raise AssertionError('my_H.dtype should be config.floatX, but they are '
                        ' %s and %s, respectively' % (my_H.dtype, config.floatX))

            allowed_v_types = ['float32']

            if config.floatX == 'float64':
                allowed_v_types.append('float64')

            assert my_V.dtype in allowed_v_types

            if config.compute_test_value != 'off':
                from theano.gof.op import PureOp
                Hv = PureOp._get_test_value(my_H)

                Vv = my_V.tag.test_value

                assert Hv.shape[0] == Vv.shape[0]

        check_H(H_hat,V)

        def make_dict():

            rval =  {
                    'G_hat' : tuple(G_hat),
                    'H_hat' : H_hat,
                    'S_hat' : S_hat,
                    'var_s0_hat' : var_s0_hat,
                    'var_s1_hat': var_s1_hat,
                    }
            if infer_labels:
                rval['Y_hat'] = Y

            return rval

        history = [ make_dict() ]

        for i, step in enumerate(self.schedule):

            if len(step) == 2:
                letter, number = step
                g_new_coeff = None
            else:
                letter, number, g_new_coeff = step
                assert letter == 'g'
                g_new_coeff = as_floatX(g_new_coeff)

            coeff = as_floatX(number)

            coeff = T.as_tensor_variable(coeff)

            coeff.name = 'coeff_step_'+str(i)

            if letter == 's':

                new_S_hat = s3c_e_step.infer_S_hat(V, H_hat, S_hat)
                new_S_hat.name = 'new_S_hat_step_'+str(i)

                if self.clip_reflections:
                    clipped_S_hat = reflection_clip(S_hat = S_hat, new_S_hat = new_S_hat, rho = self.rho)
                else:
                    clipped_S_hat = new_S_hat

                S_hat = damp(old = S_hat, new = clipped_S_hat, new_coeff = coeff)

                S_hat.name = 'S_hat_step_'+str(i)

            elif letter == 'h':

                new_H = self.infer_H_hat(V = V, H_hat = H_hat, S_hat = S_hat, G1_hat = G_hat[0])

                new_H.name = 'new_H_step_'+str(i)

                H_hat = damp(old = H_hat, new = new_H, new_coeff = coeff)
                H_hat.name = 'new_H_hat_step_'+str(i)

                check_H(H_hat,V)

            elif letter == 'g':

                if not isinstance(number, int):
                    raise ValueError("Step parameter for 'g' code must be an integer in [0, # g layers) "
                            "but got "+str(number)+" (of type "+str(type(number)))


                update = self.infer_G_hat( H_hat = H_hat, G_hat = G_hat, idx = number, Y_hat = Y)
                assert update.type.dtype == config.floatX

                if g_new_coeff is not None:
                    assert G_hat[number].type.dtype == config.floatX
                    update = damp(old = G_hat[number], new = update, new_coeff = g_new_coeff)
                    assert update.type.dtype == config.floatX

                G_hat[number] = update

                for Gv in get_debug_values(G_hat[number]):
                    assert Gv.min() >= 0.0
                    assert Gv.max() <= 1.0

                #when inferring labels, turn a G update into a G-Y-G update
                if infer_labels and number == (len(G_hat) -1):
                    update = dbm_ip.infer_Y_hat( H_hat = G_hat[-1])
                    if g_new_coeff is not None:
                        update = damp(old = Y, new = update, new_coeff = g_new_coeff)
                    Y = update

                    update = self.infer_G_hat( H_hat = H_hat, G_hat = G_hat, idx = number, Y_hat = Y)
                    if g_new_coeff is not None:
                        assert G_hat[number].type.dtype == config.floatX
                        update = damp(old = G_hat[number], new = update, new_coeff = g_new_coeff)
                    G_hat[number] = update


            else:
                raise ValueError("Invalid inference step code '"+letter+"'. Valid options are 's','h' and 'g'.")

            history.append(make_dict())

        if return_history:
            return history
        else:
            return history[-1]


def get_s3c(pddbm, W_learning_rate_scale = None):
    """ Modifies an s3c object and extracts it from a pddbm """
    rval =  pddbm.s3c
    if rval.m_step is not None:
        if W_learning_rate_scale is not None:
            rval.m_step.W_learning_rate_scale = W_learning_rate_scale
    return rval

def get_dbm(pddbm):
    return pddbm.dbm
