# Copyright (C) 2023 Thomas Hisch
#
# This file is part of saltx (https://github.com/thisch/saltx)
#
# SPDX-License-Identifier:    LGPL-3.0-or-later
"""Reproduces some of the results of "Steady-state ab initio laser theory:
Generalizations and analytic results".

See https://journals.aps.org/pra/abstract/10.1103/PhysRevA.82.063824.
"""
import logging
from collections import namedtuple
from fractions import Fraction

import matplotlib.pyplot as plt
import numpy as np
import pytest
import ufl
from dolfinx import fem
from petsc4py import PETSc
from ufl import ds, dx, inner, nabla_grad

from saltx import newtils, algorithms
from saltx.mesh import create_combined_interval_mesh, create_dcells
from saltx.lasing import NonLinearProblem
from saltx.plot import plot_ciss_eigenvalues

log = logging.getLogger(__name__)

Print = PETSc.Sys.Print


FIRST_THRESHOLD = 0.6110176
# when the 2nd mode is slightly above the threshold (see caption of figure 5)
SECOND_THRESHOLD = 0.895

# the evals were extracted (using https://apps.automeris.io/wpd/) from the centers of
# the blue crosses from Fig 3b.)
evals_of_tlms = np.array(
    [
        # k.real, k.imag
        [15.438538205980066, 0.612171052631579],
        [16.611295681063122, 0.6654605263157896],
        [14.385382059800664, 0.6674342105263159],
        [13.501661129568106, 0.8169407894736844],
        [12.528239202657808, 1.1213815789473687],
        [17.714285714285715, 0.9067434210526318],
    ]
)


@pytest.fixture
def system():
    use_pml = False
    domains = [
        # system has a length of 1.0
        (None, Fraction("0.25"), 100),  # n = 1.5, pump=D0
        (None, Fraction("0.25"), 100),  # n = 3.0, pump=D0
        (None, Fraction("0.50"), 200),  # n = 3.0, pump=0
    ]
    xstart = Fraction("0")
    if use_pml:
        domains = [
            (None, Fraction("0.8"), 800),  # PML
            (None, Fraction("0.2"), 200),
            (None, Fraction("0.25"), 200),  # n = 1.5, pump=D0
            (None, Fraction("0.25"), 200),  # n = 3.0, pump=D0
            (None, Fraction("0.50"), 400),  # n = 3.0, pump=0
            (None, Fraction("0.2"), 200),
            (None, Fraction("0.8"), 800),  # PML
        ]
        xstart = Fraction("-1")
    msh = create_combined_interval_mesh(xstart, domains)
    dcells = create_dcells(msh, xstart, domains)

    dielec = fem.Function(fem.FunctionSpace(msh, ("DG", 0)))
    pump_profile = fem.Function(fem.FunctionSpace(msh, ("DG", 0)))
    if use_pml:
        invperm = fem.Function(fem.FunctionSpace(msh, ("DG", 0)))
    else:
        invperm = 1

    ka = 15.0
    # gt is not explicitly specified in the caption of Figure 3, but the non-interacting
    # threshold (Fig 3 b) can be perfectly reproduced with gt=3.0.
    gt = 3.0

    # radius = 3.0 * gt
    radius = 1.0 * gt
    vscale = 0.5 * gt / radius
    rg_params = (ka, radius, vscale)
    Print(f"RG params: {rg_params}")
    del radius
    del vscale

    def cset(func, cells, value):
        func.x.array[cells] = np.full_like(
            cells,
            value,
            dtype=PETSc.ScalarType,
        )

    cells = dcells[2 if use_pml else 0]
    cset(dielec, cells, 1.5**2)
    cset(pump_profile, cells, 1.0)
    cells = dcells[3 if use_pml else 1]
    cset(dielec, cells, 3**2)
    cset(pump_profile, cells, 1.0)
    cells = dcells[4 if use_pml else 2]
    cset(dielec, cells, 3**2)
    cset(pump_profile, cells, 0)
    if use_pml:
        for cells in [dcells[2], dcells[3], dcells[4]]:
            cset(invperm, cells, 1.0)

        alpha_pml = 2j
        for cells in [dcells[0], dcells[6]]:
            cset(dielec, cells, 1 + alpha_pml)
            cset(invperm, cells, 1.0 / (1 + alpha_pml))
            cset(pump_profile, cells, 0.0)
        for cells in [dcells[1], dcells[5]]:
            cset(dielec, cells, 1)
            cset(invperm, cells, 1)
            cset(pump_profile, cells, 0.0)

    V = fem.FunctionSpace(msh, ("Lagrange", 3))

    evaluator = algorithms.Evaluator(
        V,
        msh,
        np.array([0.0, 1.0]),
    )

    fine_evaluator = algorithms.Evaluator(
        V,
        msh,
        np.linspace(-1, 2.0, 3 * 512) if use_pml else np.linspace(0, 1.0, 256),
    )

    # we impose the OBC at both sides therefore we don't have any DBC
    ds_obc = ufl.ds
    bcs = []

    # if use_pml:
    #     # the DBC is not really needed (I see that it has a negligible impact on the
    #     # eigenvalues).
    #     bcs_dofs = fem.locate_dofs_geometrical(
    #         V,
    #         lambda x: (np.isclose(x[0], -1.0) | np.isclose(x[0], 2.0)),
    #     )

    #     Print(f"{bcs_dofs=}")
    #     bcs = [
    #         fem.dirichletbc(PETSc.ScalarType(0), bcs_dofs, V),
    #     ]

    bcs_norm_constraint = fem.locate_dofs_geometrical(
        V,
        lambda x: x[0] > 0.75,
    )
    # I only want to impose the norm constraint on a single node
    # can this be done in a simpler way?
    bcs_norm_constraint = bcs_norm_constraint[:1]
    Print(f"{bcs_norm_constraint=}")

    n = V.dofmap.index_map.size_global
    et = PETSc.Vec().createSeq(n)
    et.setValue(bcs_norm_constraint[0], 1.0)

    fixture_locals = locals()
    nt = namedtuple("System", list(fixture_locals.keys()))(**fixture_locals)
    return nt


def plot_mode(system, mode):
    fine_mode_values = system.fine_evaluator(mode)
    fine_mode_intensity = abs(fine_mode_values) ** 2
    _, ax = plt.subplots()
    ax.plot(
        system.fine_evaluator.points,
        fine_mode_intensity,
        "x-",
    )
    ax.set_xlabel("x")
    ax.set_ylabel("Modal intensity")
    ax.axvline(x=0.0)
    ax.axvline(x=0.25)
    ax.axvline(x=0.5)
    ax.axvline(x=1.0)
    if system.use_pml:
        ax.axvline(x=-0.2)
        ax.axvline(x=1.2)
    plt.show()


def test_eval_traj(system):
    """Plot the eigenvalues as a function of D0."""
    if False:
        from saltx.plot import plot_meshfunctions

        plot_meshfunctions(
            system.msh, system.pump_profile, system.dielec, system.invperm
        )

    refine_first_mode = False

    u = ufl.TrialFunction(system.V)
    v = ufl.TestFunction(system.V)

    def assemble_form(form, diag=1.0):
        mat = fem.petsc.assemble_matrix(
            fem.form(form),
            bcs=system.bcs,
            diagonal=diag)
        mat.assemble()
        return mat

    log.info("Before first assembly")
    L = assemble_form(-system.invperm * inner(nabla_grad(u), nabla_grad(v)) * dx)
    M = assemble_form(system.dielec * inner(u, v) * dx, diag=0.0)
    R = assemble_form(inner(u, v) * ds, diag=0.0)

    nevp_inputs = algorithms.NEVPInputs(
        ka=system.ka,
        gt=system.gt,
        rg_params=system.rg_params,
        L=L,
        M=M,
        N=None,
        Q=None,
        R=R,
        bcs_norm_constraint=system.bcs_norm_constraint,
    )

    def to_const(real_value):
        return fem.Constant(system.V.mesh, complex(real_value, 0))

    if refine_first_mode:
        nlp = NonLinearProblem(
            system.V,
            system.ka,
            system.gt,
            system.et,
            dielec=system.dielec,
            n=system.n,
            use_real_jac=True,
            ds_obc=system.ds_obc,
        )
        nlp.set_pump(to_const(1.0) * system.pump_profile)
        newton_operators = newtils.create_multimode_solvers_and_matrices(
            nlp, max_nmodes=1
        )

    vals = []
    vals_after_refine = []
    for D0 in np.linspace(0.6, 1.2, 10):
        # for D0 in np.linspace(1.0, 1.6, 10):
        nevp_inputs.Q = assemble_form(
            to_const(D0) * system.pump_profile * inner(u, v) * dx, diag=0.0
        )

        modes = algorithms.get_nevp_modes(nevp_inputs, bcs=system.bcs)
        evals = np.asarray([mode.k for mode in modes])

        if False:
            for mode in modes:
                plot_mode(system, mode)

        if refine_first_mode:
            nlp.set_pump(to_const(D0) * system.pump_profile)

            mode = modes[3]  # k ~ 15 mode

            minfos = [
                newtils.NewtonModeInfo(
                    k=mode.k.real,
                    s=1.0,
                    re_array=mode.array.real,
                    im_array=mode.array.imag,
                )
            ]

            refined_mode = algorithms.refine_modes(
                minfos,
                mode.bcs,
                newton_operators[1].solver,
                nlp,
                newton_operators[1].A,
                newton_operators[1].L,
                newton_operators[1].delta_x,
                newton_operators[1].initial_x,
            )[0]

            assert refined_mode.converged

            # now we solve again the NEVP with CISS, but with a real_mode_sht
            # in the SHT
            k_sht = fem.Constant(system.msh, complex(refined_mode.k, 0))
            b_sht = fem.Function(system.V)
            # this includes the scaling term s, so we don't have to multiply it
            b_sht.x.array[:] = refined_mode.array

            # update sht term
            gk_sht = system.gt / (k_sht - system.ka + 1j * system.gt)
            Q_with_sht = fem.petsc.assemble_matrix(
                fem.form(
                    D0
                    * system.pump_profile
                    / (1 + abs(gk_sht * b_sht) ** 2)
                    * inner(u, v)
                    * dx
                ),
                bcs=system.bcs,
                diagonal=PETSc.ScalarType(0),
            )
            Q_with_sht.assemble()

            sht_modes = algorithms.get_nevp_modes(
                nevp_inputs, custom_Q=Q_with_sht, bcs=system.bcs
            )

            imag_evals = np.asarray([m.k.imag for m in sht_modes])
            number_of_modes_close_to_real_axis = np.sum(np.abs(imag_evals) < 1e-10)
            Print(
                "Number of modes close to real axis: "
                f"{number_of_modes_close_to_real_axis}"
            )
            assert number_of_modes_close_to_real_axis == 1

            number_of_modes_above_real_axis = np.sum(imag_evals > 1e-10)
            Print(f"Number of modes above real axis: {number_of_modes_above_real_axis}")

            sht_evals = np.asarray([m.k for m in sht_modes])
            vals_after_refine.append(
                np.vstack([D0 * np.ones(sht_evals.shape), sht_evals]).T
            )
        vals.append(np.vstack([D0 * np.ones(evals.shape), evals]).T)

    def scatter_plot(vals, title):
        fig, ax = plt.subplots()
        fig.suptitle(title)

        for single_d0_evals in vals:
            # remove D0 from the array
            evals = single_d0_evals[:, 1]
            # D0 is constant
            ax.plot(evals.real, evals.imag, "k-", alpha=0.2)

        merged = np.vstack(vals)
        X, Y, C = (
            merged[:, 1].real,
            merged[:, 1].imag,
            merged[:, 0].real,
        )
        norm = plt.Normalize(C.min(), C.max())

        sc = ax.scatter(X, Y, c=C, norm=norm)
        ax.set_xlabel("k.real")
        ax.set_ylabel("k.imag")

        cbar = fig.colorbar(sc, ax=ax)
        cbar.set_label("D0", loc="top")

        # TODO calculate D0 out of the eigenvalue
        ax.plot(evals_of_tlms[:, 0], np.zeros(evals_of_tlms.shape[0]), "bx")

        ax.grid(True)

    scatter_plot(vals, "Non-Interacting thresholds")

    if refine_first_mode:
        scatter_plot(
            vals_after_refine, "Thresholds when mode around k.real~15 is refined"
        )

    plt.show()


@pytest.mark.parametrize(
    "D0",
    [
        FIRST_THRESHOLD,
        0.89,  # still single mode
        SECOND_THRESHOLD,
        # 0.67, # 3 non-interacting modes
        # 0.9,  # 4 non-interacting modes
        # 0.92,  # 5 non-interacting modes
        # 1.1,  # 5 non-interacting modes
        # 1.13,  # 6 non-interacting modes
    ],
)
def test_solve(D0, system):
    u = ufl.TrialFunction(system.V)
    v = ufl.TestFunction(system.V)

    def assemble_form(form, diag=1.0):
        mat = fem.petsc.assemble_matrix(
            fem.form(form),
            bcs=system.bcs,
            diagonal=diag
        )
        mat.assemble()
        return mat

    def to_const(real_value):
        return fem.Constant(system.V.mesh, complex(real_value, 0))

    L = assemble_form(-system.invperm * inner(nabla_grad(u), nabla_grad(v)) * dx)
    M = assemble_form(system.dielec * inner(u, v) * dx, diag=0.0)
    Q = assemble_form(
        to_const(D0) * system.pump_profile * inner(u, v) * dx, diag=0.0
    )
    R = assemble_form(inner(u, v) * ds, diag=0.0)

    Print(
        f"{L.getSize()=},  DOF: {L.getInfo()['nz_used']}, MEM: {L.getInfo()['memory']}"
    )

    nevp_inputs = algorithms.NEVPInputs(
        ka=system.ka,
        gt=system.gt,
        rg_params=system.rg_params,
        L=L,
        M=M,
        N=None,
        Q=Q,
        R=R,
        bcs_norm_constraint=system.bcs_norm_constraint,
    )
    modes = algorithms.get_nevp_modes(nevp_inputs, bcs=system.bcs)
    evals = np.asarray([mode.k for mode in modes])

    nlp = NonLinearProblem(
        system.V,
        system.ka,
        system.gt,
        system.et,
        dielec=system.dielec,
        n=system.n,
        use_real_jac=True,
        ds_obc=system.ds_obc,
    )
    nlp.set_pump(to_const(D0) * system.pump_profile)

    newton_operators = newtils.create_multimode_solvers_and_matrices(nlp, max_nmodes=2)

    if False:
        modeselectors = np.argwhere(evals.imag > 0).flatten()
        for modesel in modeselectors:
            mode = modes[modesel]
            assert mode.k.imag > 0

            minfos = [
                newtils.NewtonModeInfo(
                    k=mode.k.real,
                    s=0.1,
                    re_array=mode.array.real,
                    im_array=mode.array.imag,
                )
            ]

            refined_mode = algorithms.refine_modes(
                minfos,
                mode.bcs,
                newton_operators[1].solver,
                nlp,
                newton_operators[1].A,
                newton_operators[1].L,
                newton_operators[1].delta_x,
                newton_operators[1].initial_x,
            )[0]

            if refined_mode.converged:
                mode_values = system.evaluator(refined_mode)
                mode_intensity = abs(mode_values) ** 2
                Print(f"-> {mode_intensity=}")
    else:
        multi_modes = algorithms.constant_pump_algorithm(
            modes,
            nevp_inputs,
            D0 * system.pump_profile,
            nlp,
            newton_operators,
            to_const,
            assemble_form,
            system,
            # s_init=0.1,
            first_mode_index=3,  # the first mode has k~15
        )
        multi_evals = np.asarray([mode.k for mode in multi_modes])
        number_of_modes_close_to_real_axis = np.sum(np.abs(multi_evals.imag) < 1e-10)
        assert number_of_modes_close_to_real_axis > 0
        for mode in multi_modes:
            mode_values = system.evaluator(mode)
            mode_intensity = abs(mode_values) ** 2
            Print(f"-> {mode_intensity=}")


def test_intensity_vs_pump(system):
    u = ufl.TrialFunction(system.V)
    v = ufl.TestFunction(system.V)

    def assemble_form(form, diag=1.0):
        mat = fem.petsc.assemble_matrix(
            fem.form(form),
            bcs=system.bcs,
            diagonal=diag
        )
        mat.assemble()
        return mat

    L = assemble_form(-system.invperm * inner(nabla_grad(u), nabla_grad(v)) * dx)
    M = assemble_form(system.dielec * inner(u, v) * dx, diag=0.0)
    Q = assemble_form(system.pump_profile * inner(u, v) * dx, diag=0.0)
    R = assemble_form(inner(u, v) * ds, diag=0.0)

    Print(
        f"{L.getSize()=},  DOF: {L.getInfo()['nz_used']}, MEM: {L.getInfo()['memory']}"
    )

    nevp_inputs = algorithms.NEVPInputs(
        ka=system.ka,
        gt=system.gt,
        rg_params=system.rg_params,
        L=L,
        M=M,
        N=None,
        Q=Q,
        R=R,
        bcs_norm_constraint=system.bcs_norm_constraint,
    )

    nlp = NonLinearProblem(
        system.V,
        system.ka,
        system.gt,
        system.et,
        dielec=system.dielec,
        n=system.n,
        use_real_jac=True,
        ds_obc=system.ds_obc,
    )
    newton_operators = newtils.create_multimode_solvers_and_matrices(nlp, max_nmodes=2)

    def to_const(real_value):
        return fem.Constant(system.V.mesh, complex(real_value, 0))

    aevals = []  # all eigenvalues of the modes without the SHT
    results = []  # list of (D0, intensity) tuples
    for D0 in np.linspace(0.62, 1.0, 14):
        Print(f"{D0=}")
        D0 = to_const(D0)
        nevp_inputs.Q = assemble_form(
            D0 * system.pump_profile * inner(u, v) * dx, diag=0.0
        )
        modes = algorithms.get_nevp_modes(nevp_inputs, bcs=system.bcs)
        evals = np.asarray([mode.k for mode in modes])
        assert evals.size

        nlp.set_pump(D0 * system.pump_profile)

        def check_is_single_mode(refined_mode):
            """Returns True when no other NEVP modes lie above the real
            axis."""
            # now we solve again the NEVP with CISS, but with a real_mode_sht
            # in the SHT
            k_sht = fem.Constant(system.msh, complex(refined_mode.k, 0))
            b_sht = fem.Function(system.V)
            # this includes the scaling term s, so we don't have to multiply it
            b_sht.x.array[:] = refined_mode.array

            # update sht term
            gk_sht = system.gt / (k_sht - system.ka + 1j * system.gt)
            Q_with_sht = fem.petsc.assemble_matrix(
                fem.form(
                    D0
                    * system.pump_profile
                    / (1 + abs(gk_sht * b_sht) ** 2)
                    * inner(u, v)
                    * dx
                ),
                bcs=system.bcs,
                diagonal=PETSc.ScalarType(0),
            )
            Q_with_sht.assemble()

            sht_modes = algorithms.get_nevp_modes(
                nevp_inputs, custom_Q=Q_with_sht, bcs=system.bcs
            )

            imag_evals = np.asarray([m.k.imag for m in sht_modes])
            number_of_modes_close_to_real_axis = np.sum(np.abs(imag_evals) < 1e-10)
            Print(
                "Number of modes close to real axis: "
                f"{number_of_modes_close_to_real_axis}"
            )
            assert number_of_modes_close_to_real_axis == 1

            number_of_modes_above_real_axis = np.sum(imag_evals > 1e-10)
            Print(f"Number of modes above real axis: {number_of_modes_above_real_axis}")

            return number_of_modes_above_real_axis == 0

        if False:
            # The abs evals.real condition can be used to speed up the algorithm
            modeselectors = np.argwhere(
                (evals.imag > 0)  # & (abs(evals.real - 15.4) < 0.4)
            ).flatten()

            # find single laser mode:

            # loop over all modes
            #   determine refined_mode
            #   check if this refined_mode is a single mode by checking k.imag
            #   of all modes when the refined_mode is used in the SHT term.
            # assert that only one refined_mode is a single lasing mode
            single_mode_finder_results = []
            for modesel in modeselectors:
                mode = modes[modesel]

                minfos = [
                    newtils.NewtonModeInfo(
                        k=mode.k.real,
                        s=0.1,
                        re_array=mode.array.real,
                        im_array=mode.array.imag,
                    )
                ]

                refined_mode = algorithms.refine_modes(
                    minfos,
                    mode.bcs,
                    newton_operators[1].solver,
                    nlp,
                    newton_operators[1].A,
                    newton_operators[1].L,
                    newton_operators[1].delta_x,
                    newton_operators[1].initial_x,
                )[0]

                assert refined_mode.converged
                if check_is_single_mode(refined_mode):
                    single_mode_finder_results.append(refined_mode)

            # we only expect a single active laser mode
            assert len(single_mode_finder_results) == 1
            mode_values = system.evaluator(single_mode_finder_results[0])
            mode_intensity = abs(mode_values) ** 2
            Print(f"-> {mode_intensity=}")
            results.append((D0, mode.k.real, mode_intensity.sum()))
            aevals.append(evals)
        else:
            multi_modes = algorithms.constant_pump_algorithm(
                modes,
                nevp_inputs,
                D0 * system.pump_profile,
                nlp,
                newton_operators,
                to_const,
                assemble_form,
                system,
                # s_init=0.1,
                first_mode_index=3,  # the first mode has k~15
                # todo investigate eval trajectories
            )
            multi_evals = np.asarray([mode.k for mode in multi_modes])
            number_of_modes_close_to_real_axis = np.sum(
                np.abs(multi_evals.imag) < 1e-10
            )
            assert number_of_modes_close_to_real_axis > 0
            for mode in multi_modes:
                mode_values = system.evaluator(mode)
                mode_intensity = abs(mode_values) ** 2
                Print(f"-> {mode_intensity=}")

                results.append((D0, mode.k.real, mode_intensity.sum()))
            aevals.append(multi_evals)

    _, ax = plt.subplots()
    x = np.asarray([D0._cpp_object.value for (D0, _, _) in results])
    y = np.asarray([intens for (_, _, intens) in results])
    ax.plot(
        x,
        y,
        "x",
    )
    ax.set_xlabel("Pump D0")
    ax.set_ylabel("Modal intensity at right lead + left lead")
    ax.axvline(x=FIRST_THRESHOLD)
    ax.grid(True)

    _, axes = plt.subplots(nrows=2, sharex=True)
    y = np.asarray([k for (_, k, _) in results])

    axes[0].plot(
        x[y < 16],
        y[y < 16],
        "x",
    )
    axes[0].grid(True)
    axes[0].set_xlabel("k.real Mode 1")

    axes[1].plot(
        x[y > 16],
        y[y > 16],
        "x",
    )
    axes[1].grid(True)
    axes[1].set_xlabel("k.real Mode 2")
    axes[1].set_xlabel("Pump D0")

    plot_ciss_eigenvalues(
        np.concatenate(aevals), params=system.rg_params, kagt=(system.ka, system.gt)
    )

    plt.show()
