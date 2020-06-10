# -*- coding: utf-8 -*-

# SPDX-FileCopyrightText: 2016-2020 The Atlite Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Renewable Energy Atlas Lite (Atlite)

Light-weight version of Aarhus RE Atlas for converting weather data to power
systems data
"""

import numpy as np
import pandas as pd
import xarray as xr
import xarray.ufuncs as xu
import scipy as sp
import scipy.sparse
from collections import OrderedDict
from warnings import warn
from functools import partial
from pyproj import CRS, Transformer
import geopandas as gpd
from shapely.ops import transform
import rasterio as rio
import rasterio.warp
from shapely.strtree import STRtree

import logging
logger = logging.getLogger(__name__)


def get_coords(x, y, time, dx=0.25, dy=0.25, dt='h', **kwargs):
    """
    Create an cutout coordinate system on the basis of slices and step sizes.

    Parameters
    ----------
    x : slice
        Numerical slices with lower and upper bound of the x dimension.
    y : slice
        Numerical slices with lower and upper bound of the y dimension.
    time : slice
        Slice with strings with lower and upper bound of the time dimension.
    dx : float, optional
        Step size of the x coordinate. The default is 0.25.
    dy : float, optional
        Step size of the y coordinate. The default is 0.25.
    dt : str, optional
        Frequency of the time coordinate. The default is 'h'. Valid are all
        pandas offset aliases.

    Returns
    -------
    ds : xarray.Dataset
        Dataset with x, y and time variables, representing the whole coordinate
        system.
    """
    x = slice(*sorted([x.start, x.stop]))
    y = slice(*sorted([y.start, y.stop]))

    ds = xr.Dataset({'x': np.arange(-180, 180, dx),
                     'y': np.arange(-90, 90, dy),
                     'time': pd.date_range(start="1979", end="now", freq=dt)})
    ds = ds.assign_coords(lon=ds.coords['x'], lat=ds.coords['y'])
    ds = ds.sel(x=x, y=y, time=time)
    return ds


def spdiag(v):
    N = len(v)
    inds = np.arange(N + 1, dtype=np.int32)
    return sp.sparse.csr_matrix((v, inds[:-1], inds), (N, N))


def reproject_shapes(shapes, crs1, crs2):
    """
    Project a collection of `shapes` from one crs `crs1` to
    another crs `crs2`
    """

    transformer = Transformer.from_crs(crs1, crs2)

    def _reproject_shape(shape):
        return transform(transformer.transform, shape)

    if isinstance(shapes, pd.Series):
        return shapes.map(_reproject_shape)
    elif isinstance(shapes, dict):
        return OrderedDict((k, _reproject_shape(v)) for k, v in shapes.items())
    else:
        return list(map(_reproject_shape, shapes))


def reproject(shapes, p1, p2):
    warn("reproject has been renamed to reproject_shapes", DeprecationWarning)
    return reproject_shapes(shapes, p1, p2)


reproject.__doc__ = reproject_shapes.__doc__


def grid_cell_areas(cutout):
    """
    Compute the area of each grid cell in km2
    """
    if cutout.crs == CRS.from_epsg(4326):
        # use equation derived in https://www.pmel.noaa.gov/maillists/tmap/ferret_users/fu_2004/msg00023.html
        y = cutout.coords['y']
        dy = cutout.dy
        R = 6371.0 # Authalic radius (radius for a sphere with the same surface area as the earth)
        area_km2 = (R**2 * xu.deg2rad(dx) *
                    abs(xu.sin(xu.deg2rad(y + dy / 2)) - xu.sin(xu.deg2rad(y - dy/2))))
        return area_km2
    else:
        # transform to ETRS LAEA (https://epsg.io/3035)
        grid_cells = reproject_shapes(cutout.grid_cells, cutout.crs, 3035)
        return xr.DataArray(np.array([c.area for c in grid_cells]).reshape(cutout.shape) / 1e6,
                            coords=cutout.coords, dims=['y', 'x'])


def compute_indicatormatrix(orig, dest, orig_crs=4326, dest_crs=4326):
    """
    Compute the indicatormatrix

    The indicatormatrix I[i,j] is a sparse representation of the ratio
    of the area in orig[j] lying in dest[i], where orig and dest are
    collections of polygons, i.e.

    A value of I[i,j] = 1 indicates that the shape orig[j] is fully
    contained in shape dest[j].

    Note that the polygons must be in the same crs.

    Parameters
    ---------
    orig : Collection of shapely polygons
    dest : Collection of shapely polygons

    Returns
    -------
    I : sp.sparse.lil_matrix
      Indicatormatrix
    """
    dest = dest.geometry if isinstance(dest, gpd.GeoDataFrame) else dest
    dest = reproject_shapes(dest, dest_crs, orig_crs)
    indicator = sp.sparse.lil_matrix((len(dest), len(orig)), dtype=np.float)
    tree = STRtree(orig)
    idx = dict((id(o), i) for i, o in enumerate(orig))

    for i, d in enumerate(dest):
        for o in tree.query(d):
            if o.intersects(d):
                j = idx[id(o)]
                area = d.intersection(o).area
                indicator[i, j] = area / o.area

    return indicator


def maybe_swap_spatial_dims(ds, namex='x', namey='y'):
    swaps = {}
    lx, rx = ds.indexes[namex][[0, -1]]
    ly, uy = ds.indexes[namey][[0, -1]]

    if lx > rx:
        swaps[namex] = slice(None, None, -1)
    if uy < ly:
        swaps[namey] = slice(None, None, -1)

    return ds.isel(**swaps) if swaps else ds


def _as_transform(x, y):
    lx, rx = x[[0, -1]]
    ly, uy = y[[0, -1]]

    dx = float(rx - lx) / float(len(x) - 1)
    dy = float(uy - ly) / float(len(y) - 1)

    return rio.transform.from_origin(lx, uy, dx, dy)


def regrid(ds, dimx, dimy, **kwargs):
    """
    Interpolate Dataset or DataArray `ds` to a new grid, using rasterio's
    reproject facility.

    See also: https://mapbox.github.io/rasterio/topics/resampling.html

    Parameters
    ----------
    ds : xr.Dataset|xr.DataArray
      N-dim data on a spatial grid
    dimx : pd.Index
      New x-coordinates in destination crs.
      dimx.name MUST refer to x-coord of ds.
    dimy : pd.Index
      New y-coordinates in destination crs.
      dimy.name MUST refer to y-coord of ds.
    **kwargs :
      Arguments passed to rio.wrap.reproject; of note:
      - resampling is one of gis.Resampling.{average,cubic,bilinear,nearest}
      - src_crs, dst_crs define the different crs (default: EPSG:4326)
    """
    namex = dimx.name
    namey = dimy.name

    ds = maybe_swap_spatial_dims(ds, namex, namey)

    src_transform = _as_transform(ds.indexes[namex],
                                  ds.indexes[namey])
    dst_transform = _as_transform(dimx, dimy)
    dst_shape = len(dimy), len(dimx)

    kwargs.update(dst_shape=dst_shape,
                  src_transform=src_transform,
                  dst_transform=dst_transform)
    kwargs.setdefault("src_crs", dict(init='EPSG:4326'))
    kwargs.setdefault("dst_crs", dict(init='EPSG:4326'))

    def _reproject(src, dst_shape, **kwargs):
        dst = np.empty(src.shape[:-2] + dst_shape, dtype=src.dtype)
        rio.warp.reproject(np.asarray(src), dst, **kwargs)
        return dst

    data_vars = ds.data_vars.values() if isinstance(ds, xr.Dataset) else (ds,)
    dtypes = {da.dtype for da in data_vars}
    assert len(dtypes) == 1, \
        "regrid can only reproject datasets with homogeneous dtype"

    return (xr.apply_ufunc(_reproject,
                           ds,
                           input_core_dims=[[namey, namex]],
                           output_core_dims=[['yout', 'xout']],
                           output_dtypes=[dtypes.pop()],
                           output_sizes={'yout': dst_shape[0],
                                         'xout': dst_shape[1]},
                           dask='parallelized',
                           kwargs=kwargs)
            .rename({'yout': namey, 'xout': namex})
            .assign_coords(**{namey: (namey, dimy, ds.coords[namey].attrs),
                              namex: (namex, dimx, ds.coords[namex].attrs)})
            .assign_attrs(**ds.attrs))
