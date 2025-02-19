# -*- coding: utf-8 -*-
import numpy as np
import pytest

import jax.numpy as jnp
from jax import jit, random, vmap
from jax.config import config
from jax import lax
from jax.test_util import check_grads

from caustics.extended_source_magnification import (
    _images_of_source_limb,
    _split_single_segment,
    _get_segments,
    _contours_from_open_segments,
    _connection_condition,
)
from caustics import (
    critical_and_caustic_curves,
    mag_extended_source,
)
from caustics.utils import last_nonzero

config.update("jax_platform_name", "cpu")
config.update("jax_enable_x64", True)

import MulensModel as mm

params = mm.ModelParameters({"t_0": 200.0, "u_0": 0.1, "t_E": 3.0, "rho": 0.1})
point_lens = mm.PointLens(params)


def mag_espl_lee_unif(w_points, rho):
    return point_lens.get_point_lens_uniform_integrated_magnification(
        np.abs(w_points), rho
    )


def mag_espl_lee_ld(w_points, rho, u1=0.0):
    Gamma = 2 * u1 / (3.0 - u1)
    return point_lens.get_point_lens_LD_integrated_magnification(
        np.abs(w_points), rho, Gamma
    )


def mag_vbb_binary(w0, rho, a, e1, u1=0.0, accuracy=1e-05):
    e2 = 1 - e1
    x_cm = (e1 - e2) * a
    bl = mm.BinaryLens(e2, e1, 2 * a)
    return bl.vbbl_magnification(
        w0.real - x_cm, w0.imag, rho, accuracy=accuracy, u_limb_darkening=u1
    )


def count_duplicates(a):
    u, c = jnp.unique(a, return_counts=True)
    return (c > 1).sum()


@pytest.mark.parametrize("rho", [1e-01, 1e-02, 1e-03])
def test_images_of_source_limb(rho):
    a, e1, e2, r3 = 0.698, 0.02809, 0.9687, -0.0197 - 0.95087j

    critical_curves, caustic_curves = critical_and_caustic_curves(
        nlenses=3, npts=20, a=a, e1=e1, e2=e2, r3=r3, rho=rho
    )

    key = random.PRNGKey(42)
    key, subkey1, subkey2 = random.split(key, num=3)

    # Generate random test points near the caustics
    w_test = (
        caustic_curves
        + random.uniform(subkey1, caustic_curves.shape, minval=-2 * rho, maxval=2 * rho)
        + 1j
        * random.uniform(subkey2, caustic_curves.shape, minval=-2 * rho, maxval=2 * rho)
    )
    for w in w_test:
        z, z_mask, z_parity = _images_of_source_limb(
            w, rho, nlenses=3, a=a, e1=e1, e2=e2, r3=r3, npts=400, niter=5
        )

        # Check that there are no identical points
        assert count_duplicates(z.reshape(-1)) == 0


@pytest.mark.parametrize("rho", [1.0, 1e-01, 1e-02, 1e-03])
def test_mag_extended_source_single_unif(rho, rtol=1e-03):
    npts_limb = 150
    w_points = jnp.linspace(0.0, 3.0 * rho, 11)

    mags = vmap(
        lambda w: mag_extended_source(
            w,
            rho,
            nlenses=1,
            npts_limb=npts_limb,
        )
    )(w_points)

    mags_lee = mag_espl_lee_unif(w_points, rho)
    np.testing.assert_allclose(mags, mags_lee, rtol=rtol)

    # Check that u1 = 0. reduces to uniform case
    mags2 = vmap(
        lambda w: mag_extended_source(
            w,
            rho,
            limb_darkening=True,
            u1=0.0,
            nlenses=1,
            npts_limb=npts_limb,
            npts_ld=50,
        )
    )(w_points)

    np.testing.assert_allclose(mags, mags2, rtol=rtol)


@pytest.mark.parametrize("rho", [1.0, 1e-01, 1e-02])
def test_mag_extended_source_single_ld(rho, rtol=1e-03):
    npts_limb = 300
    npts_ld = 150
    u1 = 0.7
    w_points = jnp.linspace(0.0, 3.0 * rho, 11)

    mags = vmap(
        lambda w: mag_extended_source(
            w,
            rho,
            limb_darkening=True,
            u1=u1,
            nlenses=1,
            npts_limb=npts_limb,
            npts_ld=npts_ld,
        )
    )(w_points)

    mags_lee = mag_espl_lee_ld(w_points, rho, u1=u1)
    np.testing.assert_allclose(mags, mags_lee, rtol=rtol)


def test_split_single_segment():
    x = jnp.array([0.0, 1.0, 2.0, 3.0, 0.0, 4.0, 5.0, 0.0, 0.0, 6.0]).astype(
        jnp.complex128
    )
    res = _split_single_segment(x[None, :], n_parts=5)[:, 0, :].real

    np.testing.assert_equal(
        res[0], np.array([0.0, 1.0, 2.0, 3.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    )
    np.testing.assert_equal(
        res[1], np.array([0.0, 0.0, 0.0, 0.0, 0.0, 4.0, 5.0, 0.0, 0.0, 0.0])
    )
    np.testing.assert_equal(
        res[2], np.array([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 6.0])
    )
    np.testing.assert_equal(res[3], np.zeros_like(res.shape[0]))
    np.testing.assert_equal(res[4], np.zeros_like(res.shape[0]))


@pytest.mark.parametrize("rho", [1e-01, 1e-02, 1e-03])
def test_get_segments(rho):
    a, e1, e2, r3 = 0.698, 0.02809, 0.9687, -0.0197 - 0.95087j

    _, caustic_curves = critical_and_caustic_curves(
        npts=10, nlenses=3, a=a, e1=e1, e2=e2, r3=r3
    )

    key = random.PRNGKey(42)
    key, subkey1, subkey2 = random.split(key, num=3)

    # Generate random test points near the caustics
    w_test = (
        caustic_curves
        + random.uniform(subkey1, caustic_curves.shape, minval=-2 * rho, maxval=2 * rho)
        + 1j
        * random.uniform(subkey2, caustic_curves.shape, minval=-2 * rho, maxval=2 * rho)
    )

    def f(w):
        z, z_mask, z_parity = _images_of_source_limb(
            w,
            rho,
            nlenses=3,
            a=a,
            r3=r3,
            e1=e1,
            e2=e2,
            npts=200,
        )
        segments_closed, segments_open, all_closed = _get_segments(
            z, z_mask, z_parity, nlenses=3
        )
        return segments_open

    segments_list = jnp.array([f(w) for w in w_test])

    def check_single_segment(segment):
        z, p = segment

        # Check that the parity of each nonzero point is the same
        cond_parity = jnp.logical_xor((p == -1.0).sum() > 0, (p == 1).sum() > 0)

        # Check that the segment is continous
        mask = jnp.abs(z) > 0.0
        cond_continuous = (jnp.abs(jnp.diff(mask)) > 0.0).sum() <= 1

        return lax.cond(
            jnp.logical_and(cond_parity, cond_continuous), lambda: 1.0, lambda: jnp.nan
        )

    def check_segments(segments):
        f = lambda seg: lax.cond(
            jnp.all(seg == 0 + 0j), lambda _: 1.0, check_single_segment, seg
        )
        return jnp.any(jnp.isnan(vmap(f)(segments)))

    assert jnp.all(
        jnp.isnan(jnp.array([check_segments(seg) for seg in segments_list])) == False
    )


@pytest.mark.parametrize("rho", [1e-01, 1e-02, 1e-03])
def test_get_contours(rho):
    a, e1, e2, r3 = 0.698, 0.02809, 0.9687, -0.0197 - 0.95087j

    critical_curves, caustic_curves = critical_and_caustic_curves(
        nlenses=3, npts=10, a=a, e1=e1, e2=e2, r3=r3, rho=rho
    )

    key = random.PRNGKey(42)
    key, subkey1, subkey2 = random.split(key, num=3)

    # Generate random test points near the caustics
    w_test = (
        caustic_curves
        + random.uniform(subkey1, caustic_curves.shape, minval=-2 * rho, maxval=2 * rho)
        + 1j
        * random.uniform(subkey1, caustic_curves.shape, minval=-2 * rho, maxval=2 * rho)
    )

    def f(w):
        z, z_mask, z_parity = _images_of_source_limb(
            w,
            rho,
            nlenses=3,
            a=a,
            r3=r3,
            e1=e1,
            e2=e2,
            npts=400,
            niter=10,
        )

        segments_closed, segments_open, all_closed = _get_segments(
            z, z_mask, z_parity, nlenses=3
        )
        contours, contours_p = _contours_from_open_segments(segments_open)
        tail_idcs = vmap(last_nonzero)(contours.real)
        return contours, contours_p, tail_idcs

    contours_list, contours_p_list, tail_idcs_list = [], [], []
    for w in w_test:
        contours, contours_p, tail_idcs = f(w)
        contours_list.append(contours)
        contours_p_list.append(contours_p)
        tail_idcs_list.append(tail_idcs)

    contours_list, contours_p_list, tail_idcs_list = (
        jnp.stack(contours_list),
        jnp.stack(contours_p_list),
        jnp.stack(tail_idcs_list),
    )

    tail_idcs_list = tail_idcs_list - 1

    def test_connection_cond(cont):
        """Test that contour is closed"""
        tidx = last_nonzero(jnp.abs(cont))
        return jnp.where(
            jnp.all(cont == 0 + 0j),
            True,
            _connection_condition(cont[None, :], cont[None, :], tidx, tidx, 0),
        )

    connection_conds = np.zeros((contours_list.shape[:2]))
    for i in range(contours_list.shape[0]):
        for j in range(contours_list.shape[1]):
            connection_conds[i, j] = test_connection_cond(
                contours_list[i, j],
            )

    assert jnp.all(jnp.prod(connection_conds, axis=1))


@pytest.mark.parametrize("rho", [1e-01, 1e-02, 1e-03, 1e-04])
def test_mag_extended_source_binary_unif(rho, max_err=1e-03):
    a, e1, = (
        0.45,
        0.8,
    )
    npts = 50
    critical_curves, caustic_curves = critical_and_caustic_curves(
        npts=npts, nlenses=2, a=a, e1=e1
    )

    key = random.PRNGKey(42)
    key, subkey1, subkey2 = random.split(key, num=3)

    # Generate random test points near the caustics
    w_test = (
        caustic_curves
        + random.uniform(subkey1, caustic_curves.shape, minval=-2 * rho, maxval=2 * rho)
        + 1j
        * random.uniform(subkey1, caustic_curves.shape, minval=-2 * rho, maxval=2 * rho)
    )

    # Compute the magnification with `caustics` and VBB
    def body_fn(_, w):
        mag = mag_extended_source(
            w,
            rho,
            nlenses=2,
            npts_limb=600,
            a=a,
            e1=e1,
        )
        return 0, mag

    _, mags = lax.scan(body_fn, 0, w_test)

    mags_vbb = np.array(
        [mag_vbb_binary(w, rho, a, e1, u1=0.0, accuracy=1e-05) for w in w_test]
    )

    assert np.all(np.abs((mags - mags_vbb) / mags_vbb) < max_err)


@pytest.mark.parametrize("rho", [1e-01, 1e-02])
def test_mag_extended_source_binary_ld(rho, max_err=1e-03):
    u1 = 0.7
    a, e1, = (
        0.45,
        0.8,
    )
    npts = 20
    critical_curves, caustic_curves = critical_and_caustic_curves(
        npts=npts, nlenses=2, a=a, e1=e1
    )

    key = random.PRNGKey(42)
    key, subkey1, subkey2 = random.split(key, num=3)

    # Generate random test points near the caustics
    w_test = (
        caustic_curves
        + random.uniform(subkey1, caustic_curves.shape, minval=-2 * rho, maxval=2 * rho)
        + 1j
        * random.uniform(subkey1, caustic_curves.shape, minval=-2 * rho, maxval=2 * rho)
    )

    # Compute the magnification with `caustics` and VBB
    def body_fn(_, w):
        mag = mag_extended_source(
            w,
            rho,
            u1=u1,
            nlenses=2,
            npts_limb=300,
            a=a,
            e1=e1,
            limb_darkening=True,
            npts_ld=100,
        )
        return 0, mag

    _, mags = lax.scan(body_fn, 0, w_test)

    mags_vbb = np.array(
        [mag_vbb_binary(w, rho, a, e1, u1=u1, accuracy=1e-05) for w in w_test]
    )

    assert np.all(np.abs((mags - mags_vbb) / mags_vbb) < max_err)


def test_grad_mag_extended_source_binary(rho=1e-02):
    a, e1, = (
        0.45,
        0.8,
    )
    critical_curves, caustic_curves = critical_and_caustic_curves(
        npts=1, nlenses=2, a=a, e1=e1
    )
    w0 = caustic_curves[0]

    f = lambda a: mag_extended_source(w0, rho, u=0.2, nlenses=2, a=a, e1=e1)
    check_grads(f, (a,), 1, rtol=1e-02)

    f = lambda rho: mag_extended_source(w0, rho, u=0.2, nlenses=2, a=a, e1=e1)
    check_grads(f, (rho,), 1, rtol=1e-02)
