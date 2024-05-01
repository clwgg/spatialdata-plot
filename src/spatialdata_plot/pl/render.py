from __future__ import annotations

from collections import abc
from copy import copy
from typing import Union

import dask
import datashader as ds
import geopandas as gpd
import matplotlib
import matplotlib.transforms as mtransforms
import numpy as np
import pandas as pd
import scanpy as sc
import spatialdata as sd
from anndata import AnnData
from matplotlib.cm import ScalarMappable
from matplotlib.colors import ListedColormap, Normalize
from multiscale_spatial_image.multiscale_spatial_image import MultiscaleSpatialImage
from pandas.api.types import is_categorical_dtype
from scanpy._settings import settings as sc_settings
from spatialdata._core.data_extent import get_extent
from spatialdata.models import (
    PointsModel,
)
from spatialdata.transformations import (
    get_transformation,
)

from spatialdata_plot._logging import logger
from spatialdata_plot.pl.render_params import (
    FigParams,
    ImageRenderParams,
    LabelsRenderParams,
    LegendParams,
    PointsRenderParams,
    ScalebarParams,
    ShapesRenderParams,
)
from spatialdata_plot.pl.utils import (
    _decorate_axs,
    _get_collection_shape,
    _get_colors_for_categorical_obs,
    _get_linear_colormap,
    _map_color_seg,
    _maybe_set_colors,
    _mpl_ax_contains_elements,
    _multiscale_to_spatial_image,
    _normalize,
    _rasterize_if_necessary,
    _set_color_source_vec,
    to_hex,
)
from spatialdata_plot.pp.utils import _get_instance_key, _get_region_key

_Normalize = Union[Normalize, abc.Sequence[Normalize]]


def _render_shapes(
    sdata: sd.SpatialData,
    render_params: ShapesRenderParams,
    coordinate_system: str,
    ax: matplotlib.axes.SubplotBase,
    fig_params: FigParams,
    scalebar_params: ScalebarParams,
    legend_params: LegendParams,
) -> None:
    elements = render_params.elements

    if render_params.groups is not None:
        if isinstance(render_params.groups, str):
            render_params.groups = [render_params.groups]
        if not all(isinstance(g, str) for g in render_params.groups):
            raise TypeError("All groups must be strings.")

    sdata_filt = sdata.filter_by_coordinate_system(
        coordinate_system=coordinate_system,
        filter_table=sdata.table is not None,
    )
    if isinstance(elements, str):
        elements = [elements]

    if elements is None:
        elements = list(sdata_filt.shapes.keys())

    for e in elements:
        shapes = sdata.shapes[e]
        n_shapes = sum(len(s) for s in shapes)

        if sdata.table is None:
            table = AnnData(None, obs=pd.DataFrame(index=pd.Index(np.arange(n_shapes), dtype=str)))
        else:
            table = sdata.table[sdata.table.obs[_get_region_key(sdata)].isin([e])]

        # get color vector (categorical or continuous)
        color_source_vector, color_vector, _ = _set_color_source_vec(
            sdata=sdata_filt,
            element=sdata_filt.shapes[e],
            element_name=e,
            value_to_plot=render_params.col_for_color,
            layer=render_params.layer,
            groups=render_params.groups,
            palette=render_params.palette,
            na_color=render_params.color or render_params.cmap_params.na_color,
            alpha=render_params.fill_alpha,
            cmap_params=render_params.cmap_params,
        )

        values_are_categorical = color_source_vector is not None

        # color_source_vector is None when the values aren't categorical
        if values_are_categorical and render_params.transfunc is not None:
            color_vector = render_params.transfunc(color_vector)

        norm = copy(render_params.cmap_params.norm)

        if len(color_vector) == 0:
            color_vector = [render_params.cmap_params.na_color]

        # filter by `groups`
        if render_params.groups is not None and color_source_vector is not None:
            mask = color_source_vector.isin(render_params.groups)
            shapes = shapes[mask]
            shapes = shapes.reset_index()
            color_source_vector = color_source_vector[mask]
            color_vector = color_vector[mask]

        # Using dict.fromkeys here since set returns in arbitrary order
        # remove the color of NaN values, else it might be assigned to a category
        # order of color in the palette should agree to order of occurence
        if color_source_vector is None:
            palette = ListedColormap(dict.fromkeys(color_vector))
        else:
            palette = ListedColormap(dict.fromkeys(color_vector[~pd.Categorical(color_source_vector).isnull()]))

        # Apply the transformation to the PatchCollection's paths
        trans = get_transformation(sdata_filt.shapes[e], get_all=True)[coordinate_system]
        affine_trans = trans.to_affine_matrix(input_axes=("x", "y"), output_axes=("x", "y"))
        trans = mtransforms.Affine2D(matrix=affine_trans)

        shapes = gpd.GeoDataFrame(shapes, geometry="geometry")

        # Determine which method to use for rendering
        method = render_params.method
        if method is None:
            method = "datashader" if len(shapes) > 10000 else "matplotlib"
        elif method not in ["matplotlib", "datashader"]:
            raise ValueError("Method must be either 'matplotlib' or 'datashader'.")
        logger.info(f"Using {method}")

        if method == "matplotlib":
            _cax = _get_collection_shape(
                shapes=shapes,
                s=render_params.scale,
                c=color_vector,
                render_params=render_params,
                rasterized=sc_settings._vector_friendly,
                cmap=render_params.cmap_params.cmap,
                norm=norm,
                fill_alpha=render_params.fill_alpha,
                outline_alpha=render_params.outline_alpha,
                zorder=render_params.zorder,
                # **kwargs,
            )
            cax = ax.add_collection(_cax)

            # Transform the paths in PatchCollection
            for path in _cax.get_paths():
                path.vertices = trans.transform(path.vertices)
                cax = ax.add_collection(_cax)
        elif method == "datashader":
            # Where to put this
            trans = mtransforms.Affine2D(matrix=affine_trans) + ax.transData

            extent = get_extent(sdata.shapes[e])
            x_ext = extent["x"][1]
            y_ext = extent["y"][1]
            # previous_xlim = fig_params.ax.get_xlim()
            # previous_ylim = fig_params.ax.get_ylim()
            x_range = [0, x_ext]
            y_range = [0, y_ext]
            # round because we need integers
            plot_width = int(np.round(x_range[1] - x_range[0]))
            plot_height = int(np.round(y_range[1] - y_range[0]))

            cvs = ds.Canvas(plot_width=plot_width, plot_height=plot_height, x_range=x_range, y_range=y_range)

            _geometry = shapes["geometry"]
            is_point = _geometry.type == "Point"

            # Handle circles encoded as points with radius
            if is_point.any():  # TODO
                scale = shapes[is_point]["radius"] * render_params.scale
                shapes.loc[is_point, "geometry"] = _geometry[is_point].buffer(scale)

            agg = cvs.polygons(shapes, geometry="geometry", agg=ds.count())

            # Render shapes with datashader
            if render_params.col_for_color is not None and (
                render_params.groups is None or len(render_params.groups) > 1
            ):
                agg = cvs.polygons(shapes, geometry="geometry", agg=ds.by(render_params.col_for_color, ds.count()))
            else:
                agg = cvs.polygons(shapes, geometry="geometry", agg=ds.count())

            color_key = (
                [x[:-2] for x in color_vector.categories.values]
                if (type(color_vector) == pd.core.arrays.categorical.Categorical)
                and (len(color_vector.categories.values) > 1)
                else None
            )
            ds_result = ds.tf.shade(
                agg, cmap=color_vector[0][:-2], alpha=render_params.fill_alpha * 255, color_key=color_key, min_alpha=200
            )

            # Render image
            rgba_image = np.transpose(ds_result.to_numpy().base, (0, 1, 2))
            _cax = ax.imshow(rgba_image, cmap=palette, zorder=render_params.zorder)
            _cax.set_transform(trans)
            cax = ax.add_image(_cax)

        # Sets the limits of the colorbar to the values instead of [0, 1]
        if not norm and not values_are_categorical:
            _cax.set_clim(min(color_vector), max(color_vector))

        if not (
            len(set(color_vector)) == 1 and list(set(color_vector))[0] == to_hex(render_params.cmap_params.na_color)
        ):
            # necessary in case different shapes elements are annotated with one table
            if color_source_vector is not None and render_params.col_for_color is not None:
                color_source_vector = color_source_vector.remove_unused_categories()

            # False if user specified color-like with 'color' parameter
            colorbar = False if render_params.col_for_color is None else legend_params.colorbar

            _ = _decorate_axs(
                ax=ax,
                cax=cax,
                fig_params=fig_params,
                adata=table,
                value_to_plot=render_params.col_for_color,
                color_source_vector=color_source_vector,
                palette=palette,
                alpha=render_params.fill_alpha,
                na_color=render_params.cmap_params.na_color,
                legend_fontsize=legend_params.legend_fontsize,
                legend_fontweight=legend_params.legend_fontweight,
                legend_loc=legend_params.legend_loc,
                legend_fontoutline=legend_params.legend_fontoutline,
                na_in_legend=legend_params.na_in_legend,
                colorbar=colorbar,
                scalebar_dx=scalebar_params.scalebar_dx,
                scalebar_units=scalebar_params.scalebar_units,
            )


def _render_points(
    sdata: sd.SpatialData,
    render_params: PointsRenderParams,
    coordinate_system: str,
    ax: matplotlib.axes.SubplotBase,
    fig_params: FigParams,
    scalebar_params: ScalebarParams,
    legend_params: LegendParams,
) -> None:
    elements = render_params.elements

    sdata_filt = sdata.filter_by_coordinate_system(
        coordinate_system=coordinate_system,
        filter_table=sdata.table is not None,
    )
    if isinstance(elements, str):
        elements = [elements]

    if elements is None:
        elements = list(sdata_filt.points.keys())

    for e in elements:
        points = sdata.points[e]
        col_for_color = render_params.col_for_color

        coords = ["x", "y"]
        if col_for_color is not None:
            if col_for_color not in points.columns:
                # no error in case there are multiple elements, but onyl some have color key
                msg = f"Color key '{col_for_color}' for element '{e}' not been found, using default colors."
                logger.warning(msg)
            else:
                coords += [col_for_color]

        points = points[coords].compute()
        if render_params.groups is not None and col_for_color is not None:
            points = points[points[col_for_color].isin(render_params.groups)]

        # we construct an anndata to hack the plotting functions
        adata = AnnData(
            X=points[["x", "y"]].values, obs=points[coords].reset_index(), dtype=points[["x", "y"]].values.dtype
        )

        # Convert back to dask dataframe to modify sdata
        points = dask.dataframe.from_pandas(points, npartitions=1)
        sdata_filt.points[e] = PointsModel.parse(points, coordinates={"x": "x", "y": "y"})

        if render_params.col_for_color is not None:
            cols = sc.get.obs_df(adata, render_params.col_for_color)
            # maybe set color based on type
            if is_categorical_dtype(cols):
                _maybe_set_colors(
                    source=adata,
                    target=adata,
                    key=render_params.col_for_color,
                    palette=render_params.palette,
                )

        # when user specified a single color, we overwrite na with it
        default_color = (
            render_params.color
            if render_params.col_for_color is None and render_params.color is not None
            else render_params.cmap_params.na_color
        )

        color_source_vector, color_vector, _ = _set_color_source_vec(
            sdata=sdata_filt,
            element=points,
            element_name=e,
            value_to_plot=render_params.col_for_color,
            groups=render_params.groups,
            palette=render_params.palette,
            na_color=default_color,
            alpha=render_params.alpha,
            cmap_params=render_params.cmap_params,
        )

        # color_source_vector is None when the values aren't categorical
        if color_source_vector is None and render_params.transfunc is not None:
            color_vector = render_params.transfunc(color_vector)

        trans = get_transformation(sdata.points[e], get_all=True)[coordinate_system]
        affine_trans = trans.to_affine_matrix(input_axes=("x", "y"), output_axes=("x", "y"))
        trans = mtransforms.Affine2D(matrix=affine_trans) + ax.transData

        norm = copy(render_params.cmap_params.norm)

        method = render_params.method
        if method is None:
            method = "datashader" if len(points) > 10000 else "matplotlib"
        elif method not in ["matplotlib", "datashader"]:
            raise ValueError("Method must be either 'matplotlib' or 'datashader'.")

        if method == "datashader":
            # NOTE: s in matplotlib is in units of points**2
            px = int(np.round(np.sqrt(render_params.size)))

            extent = get_extent(sdata_filt.points[e], coordinate_system=coordinate_system)
            x_ext = [min(0, extent["x"][0]), extent["x"][1]]
            y_ext = [min(0, extent["y"][0]), extent["y"][1]]
            previous_xlim = ax.get_xlim()
            previous_ylim = ax.get_ylim()
            # increase range if sth larger was rendered before
            if _mpl_ax_contains_elements(ax):
                x_ext = [min(x_ext[0], previous_xlim[0]), max(x_ext[1], previous_xlim[1])]
                if ax.yaxis_inverted():  # case for e.g. images
                    y_ext = [min(y_ext[0], previous_ylim[1]), max(y_ext[1], previous_ylim[0])]
                else:  # case for e.g. labels
                    y_ext = [min(y_ext[0], previous_ylim[0]), max(y_ext[1], previous_ylim[1])]
            # round because we need integers
            plot_width = int(np.round(x_ext[1] - x_ext[0]))
            plot_height = int(np.round(y_ext[1] - y_ext[0]))

            # use datashader for the visualization of points
            # TODO: what about trans/norm at this point?
            cvs = ds.Canvas(plot_width=plot_width, plot_height=plot_height, x_range=x_ext, y_range=y_ext)

            color_by_categorical = col_for_color is not None and points[col_for_color].values.dtype == object
            aggregate_with_sum = None
            if col_for_color is not None and (render_params.groups is None or len(render_params.groups) > 1):
                if color_by_categorical:
                    agg = cvs.points(sdata_filt.points[e], "x", "y", agg=ds.by(col_for_color, ds.count()))
                else:
                    # numerical
                    agg = cvs.points(sdata_filt.points[e], "x", "y", agg=ds.sum(column=col_for_color))
                    # save min and max values for drawing the colorbar
                    aggregate_with_sum = (agg.min(), agg.max())
            else:
                agg = cvs.points(sdata_filt.points[e], "x", "y", agg=ds.count())

            color_key = (
                [x[:-2] for x in color_vector.categories.values]
                if (type(color_vector) == pd.core.arrays.categorical.Categorical)
                and (len(color_vector.categories.values) > 1)
                else None
            )
            if color_by_categorical or col_for_color is None:
                ds_result = ds.tf.shade(
                    ds.tf.spread(agg, px=px),
                    rescale_discrete_levels=True,
                    cmap=color_vector[0][:-2],
                    color_key=color_key,
                    min_alpha=np.min([150, render_params.alpha * 255]),
                )  # TODO: choose other value than 150 for min_alpha (here and below)?
            else:
                ds_result = ds.tf.shade(
                    ds.tf.spread(agg, px=px),
                    rescale_discrete_levels=True,
                    cmap=render_params.cmap_params.cmap,
                    # color_key=color_key,
                )
            # render image
            rbga_image = np.transpose(ds_result.to_numpy().base, (0, 1, 2))
            cax = ax.imshow(rbga_image, zorder=render_params.zorder, alpha=render_params.alpha)
            if aggregate_with_sum is not None:
                cax = ScalarMappable(
                    norm=matplotlib.colors.Normalize(vmin=aggregate_with_sum[0], vmax=aggregate_with_sum[1]),
                    cmap=render_params.cmap_params.cmap,
                )

        elif method == "matplotlib":
            # update axis limits if plot was empty before (necessary if datashader comes after)
            update_parameters = not _mpl_ax_contains_elements(ax)
            # original way of plotting points
            _cax = ax.scatter(
                adata[:, 0].X.flatten(),
                adata[:, 1].X.flatten(),
                s=render_params.size,
                c=color_vector,
                rasterized=sc_settings._vector_friendly,
                cmap=render_params.cmap_params.cmap,
                norm=norm,
                alpha=render_params.alpha,
                transform=trans,
                zorder=render_params.zorder,
                # **kwargs,
            )
            cax = ax.add_collection(_cax)
            if update_parameters:
                # necessary if points are plotted with mpl first and then with datashader
                extent = get_extent(sdata_filt.points[e], coordinate_system=coordinate_system)
                ax.set_xbound(extent["x"])
                ax.set_ybound(extent["y"])

        if len(set(color_vector)) != 1 or list(set(color_vector))[0] != to_hex(render_params.cmap_params.na_color):
            if color_source_vector is None:
                palette = ListedColormap(dict.fromkeys(color_vector))
            else:
                palette = ListedColormap(dict.fromkeys(color_vector[~pd.Categorical(color_source_vector).isnull()]))

            _ = _decorate_axs(
                ax=ax,
                cax=cax,
                fig_params=fig_params,
                adata=adata,
                value_to_plot=render_params.col_for_color,
                color_source_vector=color_source_vector,
                palette=palette,
                alpha=render_params.alpha,
                na_color=render_params.cmap_params.na_color,
                legend_fontsize=legend_params.legend_fontsize,
                legend_fontweight=legend_params.legend_fontweight,
                legend_loc=legend_params.legend_loc,
                legend_fontoutline=legend_params.legend_fontoutline,
                na_in_legend=legend_params.na_in_legend,
                colorbar=legend_params.colorbar,
                scalebar_dx=scalebar_params.scalebar_dx,
                scalebar_units=scalebar_params.scalebar_units,
            )


def _render_images(
    sdata: sd.SpatialData,
    render_params: ImageRenderParams,
    coordinate_system: str,
    ax: matplotlib.axes.SubplotBase,
    fig_params: FigParams,
    scalebar_params: ScalebarParams,
    legend_params: LegendParams,
    rasterize: bool,
) -> None:
    elements = render_params.elements

    sdata_filt = sdata.filter_by_coordinate_system(
        coordinate_system=coordinate_system,
        filter_table=sdata.table is not None,
    )

    if isinstance(elements, str):
        elements = [elements]

    if elements is None:
        elements = list(sdata_filt.images.keys())

    for i, e in enumerate(elements):
        img = sdata.images[e]
        extent = get_extent(img, coordinate_system=coordinate_system)
        scale = render_params.scale[i] if isinstance(render_params.scale, list) else render_params.scale

        # get best scale out of multiscale image
        if isinstance(img, MultiscaleSpatialImage):
            img = _multiscale_to_spatial_image(
                multiscale_image=img,
                element=e,
                dpi=fig_params.fig.dpi,
                width=fig_params.fig.get_size_inches()[0],
                height=fig_params.fig.get_size_inches()[1],
                scale=scale,
            )
        # rasterize spatial image if necessary to speed up performance
        if rasterize:
            img = _rasterize_if_necessary(
                image=img,
                dpi=fig_params.fig.dpi,
                width=fig_params.fig.get_size_inches()[0],
                height=fig_params.fig.get_size_inches()[1],
                coordinate_system=coordinate_system,
                extent=extent,
            )

        if render_params.channel is None:
            channels = img.coords["c"].values
        else:
            channels = (
                [render_params.channel] if isinstance(render_params.channel, (str, int)) else render_params.channel
            )

        n_channels = len(channels)

        # True if user gave n cmaps for n channels
        got_multiple_cmaps = isinstance(render_params.cmap_params, list)
        if got_multiple_cmaps:
            logger.warning(
                "You're blending multiple cmaps. "
                "If the plot doesn't look like you expect, it might be because your "
                "cmaps go from a given color to 'white', and not to 'transparent'. "
                "Therefore, the 'white' of higher layers will overlay the lower layers. "
                "Consider using 'palette' instead."
            )

        # not using got_multiple_cmaps here because of ruff :(
        if isinstance(render_params.cmap_params, list) and len(render_params.cmap_params) != n_channels:
            raise ValueError("If 'cmap' is provided, its length must match the number of channels.")

        # prepare transformations
        trans = get_transformation(img, get_all=True)[coordinate_system]
        affine_trans = trans.to_affine_matrix(input_axes=("x", "y"), output_axes=("x", "y"))
        trans = mtransforms.Affine2D(matrix=affine_trans)
        trans_data = trans + ax.transData

        # 1) Image has only 1 channel
        if n_channels == 1 and not isinstance(render_params.cmap_params, list):
            layer = img.sel(c=channels).squeeze()

            if render_params.quantiles_for_norm != (None, None):
                layer = _normalize(
                    layer, pmin=render_params.quantiles_for_norm[0], pmax=render_params.quantiles_for_norm[1], clip=True
                )

            if render_params.cmap_params.norm is not None:  # type: ignore[attr-defined]
                layer = render_params.cmap_params.norm(layer)  # type: ignore[attr-defined]

            if render_params.palette is None:
                cmap = render_params.cmap_params.cmap  # type: ignore[attr-defined]
            else:
                cmap = _get_linear_colormap([render_params.palette], "k")[0]

            # Overwrite alpha in cmap: https://stackoverflow.com/a/10127675
            cmap._init()
            cmap._lut[:, -1] = render_params.alpha

            im = ax.imshow(
                layer,
                cmap=cmap,
                zorder=render_params.zorder,
            )
            im.set_transform(trans_data)

        # 2) Image has any number of channels but 1
        else:
            layers = {}
            for i, c in enumerate(channels):
                layers[c] = img.sel(c=c).copy(deep=True).squeeze()

                if render_params.quantiles_for_norm != (None, None):
                    layers[c] = _normalize(
                        layers[c],
                        pmin=render_params.quantiles_for_norm[0],
                        pmax=render_params.quantiles_for_norm[1],
                        clip=True,
                    )

                if not isinstance(render_params.cmap_params, list):
                    if render_params.cmap_params.norm is not None:
                        layers[c] = render_params.cmap_params.norm(layers[c])
                else:
                    if render_params.cmap_params[i].norm is not None:
                        layers[c] = render_params.cmap_params[i].norm(layers[c])

            # 2A) Image has 3 channels, no palette info, and no/only one cmap was given
            if n_channels == 3 and render_params.palette is None and not isinstance(render_params.cmap_params, list):
                if render_params.cmap_params.is_default:  # -> use RGB
                    stacked = np.stack([layers[c] for c in channels], axis=-1)
                else:  # -> use given cmap for each channel
                    channel_cmaps = [render_params.cmap_params.cmap] * n_channels
                    # Apply cmaps to each channel, add up and normalize to [0, 1]
                    stacked = (
                        np.stack([channel_cmaps[i](layers[c]) for i, c in enumerate(channels)], 0).sum(0) / n_channels
                    )
                    # Remove alpha channel so we can overwrite it from render_params.alpha
                    stacked = stacked[:, :, :3]
                    logger.warning(
                        "One cmap was given for multiple channels and is now used for each channel. "
                        "You're blending multiple cmaps. "
                        "If the plot doesn't look like you expect, it might be because your "
                        "cmaps go from a given color to 'white', and not to 'transparent'. "
                        "Therefore, the 'white' of higher layers will overlay the lower layers. "
                        "Consider using 'palette' instead."
                    )

                im = ax.imshow(
                    stacked,
                    alpha=render_params.alpha,
                    zorder=render_params.zorder,
                )
                im.set_transform(trans_data)

            # 2B) Image has n channels, no palette/cmap info -> sample n categorical colors
            elif render_params.palette is None and not got_multiple_cmaps:
                # overwrite if n_channels == 2 for intuitive result
                if n_channels == 2:
                    seed_colors = ["#ff0000ff", "#00ff00ff"]
                else:
                    seed_colors = _get_colors_for_categorical_obs(list(range(n_channels)))

                channel_cmaps = [_get_linear_colormap([c], "k")[0] for c in seed_colors]

                # Apply cmaps to each channel and add up
                colored = np.stack([channel_cmaps[i](layers[c]) for i, c in enumerate(channels)], 0).sum(0)

                # Remove alpha channel so we can overwrite it from render_params.alpha
                colored = colored[:, :, :3]

                im = ax.imshow(
                    colored,
                    alpha=render_params.alpha,
                    zorder=render_params.zorder,
                )
                im.set_transform(trans_data)

            # 2C) Image has n channels and palette info
            elif render_params.palette is not None and not got_multiple_cmaps:
                if len(render_params.palette) != n_channels:
                    raise ValueError("If 'palette' is provided, its length must match the number of channels.")

                channel_cmaps = [_get_linear_colormap([c], "k")[0] for c in render_params.palette]

                # Apply cmaps to each channel and add up
                colored = np.stack([channel_cmaps[i](layers[c]) for i, c in enumerate(channels)], 0).sum(0)

                # Remove alpha channel so we can overwrite it from render_params.alpha
                colored = colored[:, :, :3]

                im = ax.imshow(
                    colored,
                    alpha=render_params.alpha,
                    zorder=render_params.zorder,
                )
                im.set_transform(trans_data)

            elif render_params.palette is None and got_multiple_cmaps:
                channel_cmaps = [cp.cmap for cp in render_params.cmap_params]  # type: ignore[union-attr]

                # Apply cmaps to each channel, add up and normalize to [0, 1]
                colored = np.stack([channel_cmaps[i](layers[c]) for i, c in enumerate(channels)], 0).sum(0) / n_channels

                # Remove alpha channel so we can overwrite it from render_params.alpha
                colored = colored[:, :, :3]

                im = ax.imshow(
                    colored,
                    alpha=render_params.alpha,
                    zorder=render_params.zorder,
                )
                im.set_transform(trans_data)

            elif render_params.palette is not None and got_multiple_cmaps:
                raise ValueError("If 'palette' is provided, 'cmap' must be None.")


def _render_labels(
    sdata: sd.SpatialData,
    render_params: LabelsRenderParams,
    coordinate_system: str,
    ax: matplotlib.axes.SubplotBase,
    fig_params: FigParams,
    scalebar_params: ScalebarParams,
    legend_params: LegendParams,
    rasterize: bool,
) -> None:
    elements = render_params.elements

    if not isinstance(render_params.outline, bool):
        raise TypeError("Parameter 'outline' must be a boolean.")

    if not isinstance(render_params.contour_px, int):
        raise TypeError("Parameter 'contour_px' must be an integer.")

    if render_params.groups is not None:
        if isinstance(render_params.groups, str):
            render_params.groups = [render_params.groups]
        if not all(isinstance(g, str) for g in render_params.groups):
            raise TypeError("All groups must be strings.")

    sdata_filt = sdata.filter_by_coordinate_system(
        coordinate_system=coordinate_system,
        filter_table=sdata.table is not None,
    )
    if isinstance(elements, str):
        elements = [elements]

    if elements is None:
        elements = list(sdata_filt.labels.keys())

    for i, e in enumerate(elements):
        label = sdata_filt.labels[e]
        extent = get_extent(label, coordinate_system=coordinate_system)
        scale = render_params.scale[i] if isinstance(render_params.scale, list) else render_params.scale

        # get best scale out of multiscale label
        if isinstance(label, MultiscaleSpatialImage):
            label = _multiscale_to_spatial_image(
                multiscale_image=label,
                element=e,
                dpi=fig_params.fig.dpi,
                width=fig_params.fig.get_size_inches()[0],
                height=fig_params.fig.get_size_inches()[1],
                scale=scale,
                is_label=True,
            )
        # rasterize spatial image if necessary to speed up performance
        if rasterize:
            label = _rasterize_if_necessary(
                image=label,
                dpi=fig_params.fig.dpi,
                width=fig_params.fig.get_size_inches()[0],
                height=fig_params.fig.get_size_inches()[1],
                coordinate_system=coordinate_system,
                extent=extent,
            )

        if sdata.table is None:
            instance_id = np.unique(label)
            table = AnnData(None, obs=pd.DataFrame(index=np.arange(len(instance_id))))
        else:
            instance_key = _get_instance_key(sdata)
            region_key = _get_region_key(sdata)

            table = sdata.table[sdata.table.obs[region_key].isin([e])]

            # get instance id based on subsetted table
            instance_id = table.obs[instance_key].values

        trans = get_transformation(label, get_all=True)[coordinate_system]
        affine_trans = trans.to_affine_matrix(input_axes=("x", "y"), output_axes=("x", "y"))
        trans = mtransforms.Affine2D(matrix=affine_trans)
        trans_data = trans + ax.transData

        # get color vector (categorical or continuous)
        color_source_vector, color_vector, categorical = _set_color_source_vec(
            sdata=sdata_filt,
            element=label,
            element_name=e,
            value_to_plot=render_params.color,
            layer=render_params.layer,
            groups=render_params.groups,
            palette=render_params.palette,
            na_color=render_params.cmap_params.na_color,
            alpha=render_params.fill_alpha,
            cmap_params=render_params.cmap_params,
        )

        if (render_params.fill_alpha != render_params.outline_alpha) and render_params.contour_px is not None:
            # First get the labels infill and plot them
            labels_infill = _map_color_seg(
                seg=label.values,
                cell_id=instance_id,
                color_vector=color_vector,
                color_source_vector=color_source_vector,
                cmap_params=render_params.cmap_params,
                seg_erosionpx=None,
                seg_boundaries=render_params.outline,
                na_color=render_params.cmap_params.na_color,
            )

            _cax = ax.imshow(
                labels_infill,
                rasterized=True,
                cmap=None if categorical else render_params.cmap_params.cmap,
                norm=None if categorical else render_params.cmap_params.norm,
                alpha=render_params.fill_alpha,
                origin="lower",
                zorder=render_params.zorder,
            )
            _cax.set_transform(trans_data)
            cax = ax.add_image(_cax)

            # Then overlay the contour
            labels_contour = _map_color_seg(
                seg=label.values,
                cell_id=instance_id,
                color_vector=color_vector,
                color_source_vector=color_source_vector,
                cmap_params=render_params.cmap_params,
                seg_erosionpx=render_params.contour_px,
                seg_boundaries=render_params.outline,
                na_color=render_params.cmap_params.na_color,
            )

            _cax = ax.imshow(
                labels_contour,
                rasterized=True,
                cmap=None if categorical else render_params.cmap_params.cmap,
                norm=None if categorical else render_params.cmap_params.norm,
                alpha=render_params.outline_alpha,
                origin="lower",
                zorder=render_params.zorder,
            )
        else:
            # Default: no alpha, contour = infill
            label = _map_color_seg(
                seg=label.values,
                cell_id=instance_id,
                color_vector=color_vector,
                color_source_vector=color_source_vector,
                cmap_params=render_params.cmap_params,
                seg_erosionpx=render_params.contour_px,
                seg_boundaries=render_params.outline,
                na_color=render_params.cmap_params.na_color,
            )

            _cax = ax.imshow(
                label,
                rasterized=True,
                cmap=None if categorical else render_params.cmap_params.cmap,
                norm=None if categorical else render_params.cmap_params.norm,
                alpha=render_params.fill_alpha,
                origin="lower",
                zorder=render_params.zorder,
            )
        _cax.set_transform(trans_data)
        cax = ax.add_image(_cax)

        _ = _decorate_axs(
            ax=ax,
            cax=cax,
            fig_params=fig_params,
            adata=table,
            value_to_plot=render_params.color,
            color_source_vector=color_source_vector,
            palette=render_params.palette,
            alpha=render_params.fill_alpha,
            na_color=render_params.cmap_params.na_color,
            legend_fontsize=legend_params.legend_fontsize,
            legend_fontweight=legend_params.legend_fontweight,
            legend_loc=legend_params.legend_loc,
            legend_fontoutline=legend_params.legend_fontoutline,
            na_in_legend=legend_params.na_in_legend,
            colorbar=legend_params.colorbar,
            scalebar_dx=scalebar_params.scalebar_dx,
            scalebar_units=scalebar_params.scalebar_units,
            # scalebar_kwargs=scalebar_params.scalebar_kwargs,
        )
