# -*- coding: utf-8 -*-
"""
Module collecting classes and functions for dealing with (multiphase) chemical
equilibria.

.. Note::

  This module is provisional at the moment, i.e. the API is not stable and may
  break without a deprecation cycle.

"""
from __future__ import division, absolute_import

import math
import warnings
from collections import defaultdict
from itertools import product

import numpy as np

from .chemistry import ReactionSystem, equilibrium_quotient
from ._util import prodpow, get_backend, mat_dot_vec
from .util.parsing import number_to_scientific_html
from .util.pyutil import deprecated


def equilibrium_residual(rc, c0, stoich, K, activity_product=None):
    """
    Parameters
    ---------
    rc: float
        Reaction coordinate
    c0: array_like of reals
        concentrations
    stoich: tuple
        per specie stoichiometry coefficient
    K: float
        equilibrium constant
    activity_product: callable
        callback for calculating the activity product taking
        concentration as single parameter.
    """
    if not hasattr(stoich, 'ndim') or stoich.ndim == 1:
        c = c0 + stoich*rc
    else:
        c = c0 + np.dot(stoich, rc)
    Q = equilibrium_quotient(c, stoich)
    if activity_product is not None:
        Q *= activity_product(c)
    return K - Q


def get_rc_interval(stoich, c0):
    """ get reaction coordinate interval """
    limits = c0/stoich
    if np.any(limits < 0):
        upper = -np.max(limits[np.argwhere(limits < 0)])
    else:
        upper = 0

    if np.any(limits > 0):
        lower = -np.min(limits[np.argwhere(limits > 0)])
    else:
        lower = 0

    if lower is 0 and upper is 0:
        raise ValueError("0-interval")
    else:
        return lower, upper


def _solve_equilibrium_coord(c0, stoich, K, activity_product=None):
    from scipy.optimize import brentq
    mask, = np.nonzero(stoich)
    stoich_m = stoich[mask]
    c0_m = c0[mask]
    lower, upper = get_rc_interval(stoich_m, c0_m)
    # span = upper - lower
    return brentq(
        equilibrium_residual,
        lower,  # + delta_frac*span,
        upper,  # - delta_frac*span,
        (c0_m, stoich_m, K, activity_product)
    )


def solve_equilibrium(c0, stoich, K, activity_product=None):
    """
    Solve equilibrium concentrations by using scipy.optimize.brentq

    Parameters
    ----------
    c0: array_like
        Initial guess of equilibrium concentrations
    stoich: tuple
        per specie stoichiometry coefficient (law of mass action)
    K: float
        equilibrium constant
    activity_product: callable
        see ``equilibrium_residual``
    delta_frac: float
        to avoid division by zero the span of searched values for
        the reactions coordinate (rc) is shrunk by 2*delta_frac*span(rc)
    """
    stoich = np.array(stoich)
    c0 = np.array(c0)
    rc = _solve_equilibrium_coord(c0, stoich, K, activity_product)
    return c0 + rc*stoich


def composition_balance(substances, concs, composition_number):
    if not hasattr(concs, 'ndim') or concs.ndim == 1:
        res = 0
    elif concs.ndim == 2:
        res = np.zeros(concs.shape[0])
        concs = concs.T
    else:
        raise NotImplementedError
    for s, c in zip(substances, concs):
        res += s.composition.get(composition_number, 0)*c
    return res


class _NumSys(object):

    small = 0  # precipitation limit
    pre_processor = None
    post_processor = None
    internal_x0_cb = None

    def __init__(self, eqsys, rref_equil=False, rref_preserv=False,
                 backend=None, precipitates=()):
        self.eqsys = eqsys
        self.rref_equil = rref_equil
        self.rref_preserv = rref_preserv
        self.backend = get_backend(backend)
        self.precipitates = precipitates

    def _get_A_ks(self, eq_params):
        non_precip_rids = self.eqsys.non_precip_rids(self.precipitates)
        return self.eqsys.stoichs_constants(
            self.eqsys.eq_constants(non_precip_rids, eq_params, self.small),
            self.rref_equil, backend=self.backend, non_precip_rids=non_precip_rids)

    def _inits_and_eq_params(self, params):
        return params[:self.eqsys.ns], params[self.eqsys.ns:]


class NumSysLin(_NumSys):

    def internal_x0_cb(self, init_concs, params):
        # reduce risk of stationary starting point
        return (99*init_concs + self.eqsys.dissolved(init_concs))/100

    def f(self, yvec, params):
        from pyneqsys.symbolic import linear_exprs
        init_concs, eq_params = self._inits_and_eq_params(params)
        A, ks = self._get_A_ks(eq_params)
        # yvec == C
        f_equil = [q/k - 1 if k != 0 else q for q, k
                   in zip(prodpow(yvec, A), ks)]
        B, comp_nrs = self.eqsys.composition_balance_vectors()
        f_preserv = linear_exprs(B, yvec, mat_dot_vec(B, init_concs),
                                 rref=self.rref_preserv)
        return f_equil + f_preserv


class _NumSysLinNegPenalty(NumSysLin):

    def f(self, yvec, params):
        import sympy as sp
        f_penalty = [sp.Piecewise((yi**2, yi < 0), (0, True)) for yi in yvec]
        return super(_NumSysLinNegPenalty, self).f(yvec, params) + f_penalty


class NumSysLinRel(NumSysLin):

    def max_concs(self, params, min_=min, dtype=np.float64):
        init_concs = params[:self.eqsys.ns]
        return self.eqsys.upper_conc_bounds(init_concs, min_=min_, dtype=dtype)

    def pre_processor(self, x, params):
        return x/self.max_concs(params), params

    def post_processor(self, x, params):
        return x*self.max_concs(params), params

    def f(self, yvec, params):
        import sympy as sp
        return NumSysLin.f(self, [m*yi for m, yi in zip(
            self.max_concs(params, min_=lambda x: sp.Min(*x), dtype=object),
            yvec)], params)


class NumSysSquare(NumSysLin):

    small = 1e-35

    def pre_processor(self, x, params):
        return (np.sqrt(np.abs(x)), params)

    def post_processor(self, x, params):
        return x**2, params

    def internal_x0_cb(self, init_concs, params):
        return np.sqrt(np.abs(init_concs))

    def f(self, yvec, params):
        ysq = [yi*yi for yi in yvec]
        return NumSysLin.f(self, ysq, params)


class NumSysLinTanh(NumSysLin):

    def pre_processor(self, x, params):
        ymax = self.eqsys.upper_conc_bounds(params[:self.eqsys.ns])
        return np.arctanh((8*x/ymax - 4) / 5), params

    def post_processor(self, x, params):
        ymax = self.eqsys.upper_conc_bounds(params[:self.eqsys.ns])
        return ymax*(4 + 5*np.tanh(x))/8, params

    def internal_x0_cb(self, init_concs, params):
        return self.pre_processor(init_concs, init_concs)[0]

    def f(self, yvec, params):
        import sympy
        ymax = self.eqsys.upper_conc_bounds(
            params[:self.eqsys.ns],
            min_=lambda a, b: sympy.Piecewise((a, a < b), (b, True)))
        ytanh = [yimax*(4 + 5*sympy.tanh(yi))/8
                 for yimax, yi in zip(ymax, yvec)]
        return NumSysLin.f(self, ytanh, params)


class NumSysLog(_NumSys):

    small = math.exp(-80)  # anything less than `small` is insignificant

    def pre_processor(self, x, params):
        return (np.log(np.asarray(x) + NumSysLog.small),  # 10: damping
                params)  # zero conc. ~= small

    def post_processor(self, x, params):
        return np.exp(x), params

    def internal_x0_cb(self, init_concs, params):
        # return [1]*len(init_concs)
        return [0.1]*len(init_concs)

    def f(self, yvec, params):
        from pyneqsys.symbolic import linear_exprs
        init_concs, eq_params = self._inits_and_eq_params(params)
        A, ks = self._get_A_ks(eq_params)
        # yvec == ln(C)
        f_equil = mat_dot_vec(A, yvec, [-self.backend.log(k) for k in ks])
        B, comp_nrs = self.eqsys.composition_balance_vectors()
        f_preserv = linear_exprs(B, list(map(self.backend.exp, yvec)),
                                 mat_dot_vec(B, init_concs),
                                 rref=self.rref_preserv)
        return f_equil + f_preserv


class EqCalcResult(object):

    attrs = {
        'sane': bool, 'success': bool,
        'nfev': int, 'njev': int,
        'time_cpu': float, 'time_wall': float
    }

    def __init__(self, eqsys, init_concs, varied):
        self.eqsys = eqsys
        self.all_inits, self.varied_keys = self.eqsys.per_substance_varied(init_concs, varied)
        self.conc = np.empty_like(self.all_inits)
        for k, v in self.attrs.items():
            setattr(self, k, np.zeros(self.all_inits.shape[:-1], dtype=v))

    def solve(self, **kwargs):
        for index in product(*map(range, self.all_inits.shape[:-1])):
            slc = tuple(index) + (slice(None),)
            self.conc[slc], nfo, sane = self.eqsys._solve(self.all_inits[slc], **kwargs)
            self.sane[index] = sane

            def _get(k):
                try:
                    return nfo[k]
                except TypeError:
                    return nfo[-1][k]

            for k in self.attrs:
                if k == 'sane':
                    continue
                try:
                    getattr(self, k)[index] = _get(k)
                except KeyError:
                    pass

    def _repr_html_(self):
        def fmt(num):
            return number_to_scientific_html(num, '%.5e')
        if len(self.varied_keys) == 0:
            raise NotImplementedError()
        elif len(self.varied_keys) == 1:
            var_html = self.eqsys.substances[self.varied_keys[0]].html_name
            header = ["[%s]<sub>0</sub>" % var_html] + ["[%s]" % s.html_name for s in self.eqsys.substances.values()]

            def row(i):
                j = self.eqsys.as_substance_index(self.varied_keys[0])
                return map(fmt, [self.all_inits[i, j]] + self.conc[i, :].tolist())
            pre = "  <td style='font-weight: bold;'>\n      "
            linker = "\n    </td>\n    <td>\n      "
            post = "\n    </td>"
            rows = [pre + linker.join(row(i)) + post for i in range(self.all_inits.shape[0])]
            template = """<table>\n  <tr>\n    <th>\n    %s\n    </th>\n  </tr>\n  <tr>\n  %s\n  </tr>\n</table>"""
            head_linker = "\n    </th>\n    <th>\n      "
            row_linker = "\n  </tr>\n  <tr>\n  "
            return template % (head_linker.join(header), row_linker.join(rows))
        else:
            raise NotImplementedError()

    def plot(self, ls=('-', '--', ':', '-.'), c=('k', 'r', 'g', 'b', 'c', 'm', 'y'), latex=None):
        import matplotlib.pyplot as plt
        if latex is None:
            latex = next(iter(self.eqsys.substances.values())).latex_name is not None
        if len(self.varied_keys) == 0:
            raise NotImplementedError()
        elif len(self.varied_keys) == 1:
            x = self.all_inits[:, self.eqsys.as_substance_index(self.varied_keys[0])]
            for idx, (k, v) in enumerate(self.eqsys.substances.items()):
                lbl = (r'$\mathrm{' + v.latex_name + '}$') if latex else v.name
                plt.plot(x, self.conc[:, idx], label=lbl, ls=ls[idx % len(ls)], c=c[idx % len(c)])

            ax = plt.gca()

            # Log-log
            ax.set_xscale('log')
            ax.set_yscale('log')

            # Axis labels
            var_latex = self.eqsys.substances[self.varied_keys[0]].latex_name
            ax.set_xlabel((r"$[\mathrm{%s}]_0$" if latex else "[%s]0") % var_latex)
            ax.set_ylabel("Concentration")

            # Outside legend
            box = ax.get_position()
            ax.set_position([box.x0, box.y0, box.width * 0.75, box.height])
            # Put a legend to the right of the current axis
            ax.legend(loc='upper left', bbox_to_anchor=(1, 1))
        else:
            raise NotImplementedError()


class EqSystem(ReactionSystem):

    def eq_constants(self, non_precip_rids=(), eq_params=None, small=0):
        if eq_params is None:
            eq_params = [eq.param for eq in self.rxns]
        return np.array([small if idx in non_precip_rids else
                         eq_params[idx] for idx, eq in enumerate(eq_params)])

    def upper_conc_bounds(self, init_concs, min_=min, dtype=np.float64):
        init_concs_arr = self.as_per_substance_array(init_concs, dtype=dtype)
        composition_conc = defaultdict(float)
        for conc, s_obj in zip(init_concs_arr, self.substances.values()):
            for comp_nr, coeff in s_obj.composition.items():
                if comp_nr == 0:  # charge may be created (if compensated)
                    continue
                composition_conc[comp_nr] += coeff*conc
        bounds = []
        for s_obj in self.substances.values():
            choose_from = []
            for comp_nr, coeff in s_obj.composition.items():
                if comp_nr == 0:
                    continue
                choose_from.append(composition_conc[comp_nr]/coeff)
            bounds.append(min_(choose_from))
        return bounds

    def equilibrium_quotients(self, concs):
        stoichs = self.stoichs()
        return [equilibrium_quotient(concs, stoichs[ri, :])
                for ri in range(self.nr)]

    def stoichs_constants(self, eq_params, rref=False, Matrix=None,
                          backend=None, non_precip_rids=()):
        if rref:
            from pyneqsys.symbolic import linear_rref
            be = get_backend(backend)
            rA, rb = linear_rref(self.stoichs(non_precip_rids),
                                 list(map(be.log, eq_params)),
                                 Matrix)
            return rA.tolist(), list(map(be.exp, rb))
        else:
            return (self.stoichs(non_precip_rids),
                    eq_params)

    def composition_conservation(self, concs, init_concs):
        composition_vecs, comp_keys = self.composition_balance_vectors()
        A = np.array(composition_vecs)
        return (comp_keys,
                np.dot(A, self.as_per_substance_array(concs).T),
                np.dot(A, self.as_per_substance_array(init_concs).T))

    def other_phase_species_idxs(self, phase_idx=0):
        return [idx for idx, s in enumerate(
            self.substances.values()) if s.phase_idx != phase_idx]

    @property
    @deprecated(last_supported_version='0.3.1', will_be_missing_in='0.5.0',
                use_instead=other_phase_species_idxs)
    def precipitate_substance_idxs(self):
        return [idx for idx, s in enumerate(
            self.substances.values()) if s.precipitate]

    def phase_transfer_reaction_idxs(self, phase_idx=0):
        return [idx for idx, rxn in enumerate(self.rxns)
                if rxn.has_precipitates(self.substances)]

    @property
    @deprecated(last_supported_version='0.3.1', will_be_missing_in='0.5.0',
                use_instead=phase_transfer_reaction_idxs)
    def precipitate_rxn_idxs(self):
        return [idx for idx, rxn in enumerate(self.rxns)
                if rxn.has_precipitates(self.substances)]

    def dissolved(self, concs):
        """ Return dissolved concentrations """
        new_concs = concs.copy()
        for r in self.rxns:
            if r.has_precipitates(self.substances):
                net_stoich = np.asarray(r.net_stoich(self.substances))
                s_net, s_stoich, s_idx = r.precipitate_stoich(self.substances)
                new_concs -= new_concs[s_idx]/s_stoich * net_stoich
        return new_concs

    def _fw_cond_factory(self, ri, rtol=1e-14):
        rxn = self.rxns[ri]

        def fw_cond(x, p):
            precip_stoich_coeff, precip_idx = rxn.precipitate_stoich(self.substances)[1:3]
            q = rxn.Q(self.substances, self.dissolved(x))
            k = rxn.K()
            if precip_stoich_coeff > 0:
                return q*(1+rtol) < k
            elif precip_stoich_coeff < 0:
                return q > k*(1+rtol)
            else:
                raise NotImplementedError
        return fw_cond

    def _bw_cond_factory(self, ri, small):
        rxn = self.rxns[ri]

        def bw_cond(x, p):
            precipitate_idx = rxn.precipitate_stoich(self.substances)[2]
            if x[precipitate_idx] < small:
                return False
            else:
                return True
        return bw_cond

    def _SymbolicSys_from_NumSys(self, NS, conds, rref_equil, rref_preserv):
        from pyneqsys.symbolic import SymbolicSys
        import sympy as sp
        ns = NS(self, backend=sp, rref_equil=rref_equil,
                rref_preserv=rref_preserv, precipitates=conds)
        symb_kw = {}
        if ns.pre_processor is not None:
            symb_kw['pre_processors'] = [ns.pre_processor]
        if ns.post_processor is not None:
            symb_kw['post_processors'] = [ns.post_processor]
        if ns.internal_x0_cb is not None:
            symb_kw['internal_x0_cb'] = ns.internal_x0_cb
        return SymbolicSys.from_callback(
            ns.f, self.ns, nparams=self.ns + self.nr, **symb_kw)

    def get_neqsys_conditional_chained(self, init_concs, rref_equil=False,
                                       rref_preserv=False, NumSys=NumSysLin):
        from pyneqsys import ConditionalNeqSys, ChainedNeqSys

        def factory(conds):
            return ChainedNeqSys([self._SymbolicSys_from_NumSys(
                NS, conds, rref_equil, rref_preserv) for NS in NumSys])

        cond_cbs = [(self._fw_cond_factory(ri),
                     self._bw_cond_factory(ri, NumSys[0].small)) for
                    ri in self.phase_transfer_reaction_idxs()]
        return ConditionalNeqSys(cond_cbs, factory)

    def get_neqsys_chained_conditional(self, init_concs, rref_equil=False,
                                       rref_preserv=False,
                                       NumSys=NumSysLin):
        from pyneqsys import ConditionalNeqSys, ChainedNeqSys

        def mk_factory(NS):
            def factory(conds):
                return self._SymbolicSys_from_NumSys(NS, conds, rref_equil,
                                                     rref_preserv)
            return factory

        return ChainedNeqSys(
            [ConditionalNeqSys(
                [(self._fw_cond_factory(ri),
                  self._bw_cond_factory(ri, NS.small)) for
                 ri in self.phase_transfer_reaction_idxs()],
                mk_factory(NS)
            ) for NS in NumSys])

    def get_neqsys_static_conditions(self, init_concs, rref_equil=False,
                                     rref_preserv=False,
                                     NumSys=NumSysLin, precipitates=None):
        if precipitates is None:
            precipitates = (False,)*len(self.phase_transfer_reaction_idxs())
        from pyneqsys import ChainedNeqSys
        return ChainedNeqSys([self._SymbolicSys_from_NumSys(
            NS, precipitates, rref_equil, rref_preserv) for NS in NumSys])

    def get_neqsys(self, neqsys_type, init_concs, NumSys=NumSysLin, **kwargs):
        new_kw = {'rref_equil': False, 'rref_preserv': False}
        if neqsys_type == 'static_conditions':
            new_kw['precipitates'] = None
        for k in new_kw:
            if k in kwargs:
                new_kw[k] = kwargs.pop(k)

        try:
            NumSys[0]
        except TypeError:
            new_kw['NumSys'] = (NumSys,)
        else:
            new_kw['NumSys'] = NumSys

        return getattr(self, 'get_neqsys_' + neqsys_type)(init_concs, **new_kw)

    def non_precip_rids(self, precipitates):
        return [idx for idx, precip in zip(
            self.phase_transfer_reaction_idxs(), precipitates) if not precip]

    def _result_is_sane(self, init_concs, x):
        sc_upper_bounds = np.array(self.upper_conc_bounds(init_concs))
        neg_conc, too_much = np.any(x < 0), np.any(
            x > sc_upper_bounds*(1 + 1e-12))
        if neg_conc or too_much:
            if neg_conc:
                warnings.warn("Negative concentration")
            if too_much:
                warnings.warn("Too much of at least one component")
            return False
        return True

    def _solve(self, init_concs, x0=None, NumSys=(NumSysLog, NumSysLin), neqsys='chained_conditional', **kwargs):
        if isinstance(neqsys, str):
            neqsys = self.get_neqsys(
                neqsys, init_concs, NumSys=NumSys,
                rref_equil=kwargs.pop('rref_equil', False),
                rref_preserv=kwargs.pop('rref_preserv', False),
                precipitates=kwargs.pop('precipitates', None))
        if x0 is None:
            x0 = init_concs
        params = np.concatenate((init_concs, [float(elem) for elem
                                              in self.eq_constants()]))
        x, sol = neqsys.solve(x0, params, **kwargs)
        if not sol['success']:
            warnings.warn("Root-finding indicated as failed by solver.")
        sane = self._result_is_sane(init_concs, x)
        return x, sol, sane

    def solve(self, init_concs, varied=None, **kwargs):
        results = EqCalcResult(self, init_concs, varied)
        results.solve()
        return results

    def root(self, init_concs, x0=None, neqsys=None, NumSys=NumSysLog,
             neqsys_type='chained_conditional', **kwargs):
        init_concs = self.as_per_substance_array(init_concs)
        params = np.concatenate((init_concs, [float(elem) for elem
                                              in self.eq_constants()]))
        if neqsys is None:
            neqsys = self.get_neqsys(
                neqsys_type, init_concs, NumSys=NumSys,
                rref_equil=kwargs.pop('rref_equil', False),
                rref_preserv=kwargs.pop('rref_preserv', False),
                precipitates=kwargs.pop('precipitates', None))
        if x0 is None:
            x0 = init_concs
        x, sol = neqsys.solve(x0, params, **kwargs)
        if not sol['success']:
            warnings.warn("Root finding indicated as failed by solver.")
        sane = self._result_is_sane(init_concs, x)
        return x, sol, sane

    @staticmethod
    def _get_default_plot_ax(subplot_kwargs=None):
        import matplotlib.pyplot as plt
        if subplot_kwargs is None:
            subplot_kwargs = dict(xscale='log', yscale='log')
        return plt.subplot(1, 1, 1, **subplot_kwargs)

    def substance_labels(self, latex=False):
        if latex:
            result = ['$' + s.latex_name + '$'
                      for s in self.substances.values()]
            return result
        else:
            return [s.name for s in self.substances.values()]

    def roots(self, init_concs, varied_data, varied, x0=None,
              NumSys=NumSysLog, plot_kwargs=None,
              neqsys_type='chained_conditional', **kwargs):
        """
        Parameters
        ----------
        init_concs: array or dict
        varied_data: array
        varied_idx: int or str
        x0: array
        NumSys: _NumSys subclass
            See :class:`NumSysLin`, :class:`NumSysLog`, etc.
        plot_kwargs: dict
            See py:meth:`pyneqsys.NeqSys.solve`. Two additional keys
            are intercepted here:
                latex_names: bool (default: False)
                conc_unit_str: str (default: 'M')
        neqsys_type: str
            what method to use for NeqSys construction (get_neqsys_*)
        \*\*kwargs:
            kwargs passed on to py:meth:`pyneqsys.NeqSys.solve_series`
        """
        _plot = plot_kwargs is not None
        if _plot:
            latex_names = plot_kwargs.pop('latex_names', False)
            conc_unit_str = plot_kwargs.pop('conc_unit_str', 'M')
            if 'ax' not in plot_kwargs:
                plot_kwargs['ax'] = self._get_default_plot_ax()

        init_concs = self.as_per_substance_array(init_concs)
        neqsys = self.get_neqsys(
            neqsys_type, init_concs, NumSys=NumSys,
            rref_equil=kwargs.pop('rref_equil', False),
            rref_preserv=kwargs.pop('rref_preserv', False),
            precipitates=kwargs.pop('precipitates', None))
        if x0 is None:
            x0 = init_concs

        if _plot:
            cb = neqsys.solve_and_plot_series
            if 'plot_kwargs' not in kwargs:
                kwargs['plot_kwargs'] = {}
            if 'labels' not in kwargs['plot_kwargs']:
                kwargs['plot_kwargs']['labels'] = (
                    self.substance_labels(latex_names))
            if 'substances' in plot_kwargs:
                if 'indices' in plot_kwargs:
                    raise ValueError("Now I am confused..")
                kwargs['plot_kwargs']['indices'] = map(
                    self.as_substance_index, plot_kwargs.pop('substances'))
                print(kwargs['plot_kwargs']['indices'])
        else:
            cb = neqsys.solve_series

        params = np.concatenate((init_concs, self.eq_constants()))
        xvecs, info_dicts = cb(
            x0, params, varied_data, self.as_substance_index(varied),
            propagate=False, **kwargs)
        sanity = [self._result_is_sane(init_concs, x) for x in xvecs]

        if _plot:
            import matplotlib.pyplot as plt
            from pyneqsys.plotting import mpl_outside_legend
            mpl_outside_legend(plt.gca())
            varied_subst = self.substances[varied]
            xlbl = ('$[' + varied_subst.latex_name + ']_0$' if latex_names
                    else str(varied_subst))
            plt.gca().set_xlabel(xlbl + ' / ' + conc_unit_str)
            plt.gca().set_ylabel('Concentration / ' + conc_unit_str)

        return xvecs, info_dicts, sanity

    def plot_errors(self, concs, init_concs, varied_data, varied, axes=None,
                    compositions=True, Q=True, subplot_kwargs=None):
        if axes is None:
            import matplotlib.pyplot as plt
            if subplot_kwargs is None:
                subplot_kwargs = dict(xscale='log')
            fig, axes = plt.subplots(1, 2, figsize=(10, 4),
                                     subplot_kw=subplot_kwargs)
        varied_idx = self.as_substance_index(varied)
        ls, c = '- -- : -.'.split(), 'krgbcmy'
        all_inits = np.tile(self.as_per_substance_array(init_concs),
                            (len(varied_data), 1))
        all_inits[:, varied_idx] = varied_data
        if compositions:
            cmp_nrs, m1, m2 = self.composition_conservation(concs, all_inits)
            for cidx, (cmp_nr, a1, a2) in enumerate(zip(cmp_nrs, m1, m2)):
                axes[0].plot(concs[:, varied_idx],
                             a1-a2, label='Comp ' + str(cmp_nr),
                             ls=ls[cidx % len(ls)], c=c[cidx % len(c)])
                axes[1].plot(concs[:, varied_idx],
                             (a1-a2)/np.abs(a2), label='Comp ' + str(cmp_nr),
                             ls=ls[cidx % len(ls)], c=c[cidx % len(c)])

        if Q:
            # TODO: handle precipitate phases in plotting Q error
            qs = self.equilibrium_quotients(concs)
            ks = [rxn.param for rxn in self.rxns]
            for idx, (q, k) in enumerate(zip(qs, ks)):
                axes[0].plot(concs[:, varied_idx],
                             q-k, label='K R:' + str(idx),
                             ls=ls[(idx+cidx) % len(ls)],
                             c=c[(idx+cidx) % len(c)])
                axes[1].plot(concs[:, varied_idx],
                             (q-k)/k, label='K R:' + str(idx),
                             ls=ls[(idx+cidx) % len(ls)],
                             c=c[(idx+cidx) % len(c)])

        from pyneqsys.plotting import mpl_outside_legend
        mpl_outside_legend(axes[0])
        mpl_outside_legend(axes[1])
        axes[0].set_title("Absolute errors")
        axes[1].set_title("Relative errors")
