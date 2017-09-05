#!/bin/python
# Function to apply TelescopeState calibration to visibilities
# """
# Hold, interpolate and apply cal solutions from a katdal object.
# Code largely pilfered from the katdal_loader in katsdpimager, but using a katdal object
# rather than a filename, so that things are a bit more portable.
# """

import numpy as np
import scipy.interpolate

try:
    import cPickle as pickle
except ImportError:
    import pickle

from .lazy_indexer import LazyTransform

class CalibrationReadError(RuntimeError):
    """An error occurred in loading calibration values from file"""
    pass


class ComplexInterpolate1D(object):
    """Interpolator that separates magnitude and phase of complex values.

    The phase interpolation is done by first linearly interpolating the
    complex values, then normalising. This is not perfect because the angular
    velocity changes (slower at the ends and faster in the middle), but it
    avoids the loss of amplitude that occurs without normalisation.

    The parameters are the same as for :func:`scipy.interpolate.interp1d`,
    except that fill values other than nan and "extrapolate" should not be
    used.
    """
    def __init__(self, x, y, *args, **kwargs):
        mag = np.abs(y)
        phase = y / mag
        self._mag = scipy.interpolate.interp1d(x, mag, *args, **kwargs)
        self._phase = scipy.interpolate.interp1d(x, phase, *args, **kwargs)

    def __call__(self, x):
        mag = self._mag(x)
        phase = self._phase(x)
        return phase / np.abs(phase) * mag


def interpolate_nans_1d(y, *args, **kwargs):
    if np.isnan(y).all():
        return y # nothing to do , all nan values
    if np.isfinite(y).all():
        return y # nothing to do, all values valid

    # interpolate across nans (but you will loose the first and last values)
    nan_locs = np.isnan(y)
    X = np.nonzero(~nan_locs)[0]
    Y = y[X]
    f=ComplexInterpolate1D(X, Y, *args, **kwargs)
    y=f(range(len(y)))
    return y


def _get_cal_attr(key, katdal_obj, sensor=True):
    """Load a fixed attribute from file.
    If the attribute is presented as a sensor, it is checked to ensure that
    all the values are the same.
    Raises
    ------
    CalibrationReadError
        if there was a problem reading the value from file (sensor does not exist,
        does not unpickle correctly, inconsistent values etc)
    """
    try:
        value = katdal_obj.file['TelescopeState/{}'.format(key)]['value']
        if len(value) == 0:
            raise ValueError('empty sensor')
        value = [pickle.loads(x) for x in value]
    except (NameError, SyntaxError):
        raise
    except Exception as e:
        raise CalibrationReadError('Could not read {}: {}'.format(key, e))

    if not sensor:
        timestamps = katdal_obj.file['TelescopeState/{}'.format(key)]['timestamp']
        return timestamps, value

    if not all(np.array_equal(value[0], x) for x in value):
        raise CalibrationReadError('Could not read {}: inconsistent values'.format(key))
    return value[0]


def _get_cal_antlist(katdal_obj):
    """Load antenna list used for calibration.
    If the value does not match the antenna list in the katdal dataset,
    a :exc:`CalibrationReadError` is raised. Eventually this could be
    extended to allow for an antenna list that doesn't match by permuting
    the calibration solutions.
    """
    cal_antlist = _get_cal_attr('cal_antlist', katdal_obj)
    if cal_antlist != [ant.name for ant in katdal_obj.ants]:
        raise CalibrationReadError('cal_antlist does not match katdal antenna list')
    return cal_antlist


def _get_cal_pol_ordering(katdal_obj):
    """Load polarization ordering used by calibration solutions.

    Returns
    -------
    dict
        Keys are 'h' and 'v' and values are 0 and 1, in some order
    """
    cal_pol_ordering = _get_cal_attr('cal_pol_ordering', katdal_obj)
    try:
        cal_pol_ordering = np.array(cal_pol_ordering)
    except (NameError, SyntaxError):
        raise
    except Exception as e:
        raise CalibrationReadError(str(e))
    if cal_pol_ordering.shape != (4, 2):
        raise CalibrationReadError('cal_pol_ordering does not have expected shape')
    if cal_pol_ordering[0, 0] != cal_pol_ordering[0, 1]:
        raise CalibrationReadError('cal_pol_ordering[0] is not consistent')
    if cal_pol_ordering[1, 0] != cal_pol_ordering[1, 1]:
        raise CalibrationReadError('cal_pol_ordering[1] is not consistent')
    order = [cal_pol_ordering[0, 0], cal_pol_ordering[1, 0]]
    if set(order) != set('vh'):
        raise CalibrationReadError('cal_pol_ordering does not contain h and v')
    return {order[0]: 0, order[1]: 1}


def _get_cal_product(key, katdal_obj, **kwargs):
    """Loads calibration solutions from a katdal file.

    If an error occurs while loading the data, a warning is printed and the
    return value is ``None``. Any keyword args are passed to
    :func:`scipy.interpolate.interp1d` or `ComplexInterpolate1D`.

    Solutions that contain non-finite values are discarded.

    Parameters
    ----------
    key : str
        Name of the telescope state sensor

    Returns
    -------
    interp : callable
        Interpolation function which accepts timestamps and returns
        interpolated data with shape (time, channel, pol, antenna). If the
        solution is channel-independent, that axis will be present with
        size 1.
    """
    timestamps, values = _get_cal_attr(key, katdal_obj, sensor=False)
    values = np.asarray(values)

    if (timestamps[-1] < katdal_obj.timestamps[0]):
        print('All %s calibration solution ahead of observation, no overlap' % key)
    elif (timestamps[0] > katdal_obj.timestamps[-1]):
        print('All %s calibration solution after observation, no overlap' % key)

    if values.ndim == 3:
        # Insert a channel axis
        values = values[:, np.newaxis, ...]

    if values.ndim != 4:
        raise ValueError('Calibration solutions has wrong number of dimensions')

    # only use solutions with valid values, assuming matrix dimensions
    # (ts, chan, pol, ant)
    # - all values per antenna must be valid
    # - all values per polarisation must be valid
    # - some channels must be valid (you will interpolate over them later)
    ts_mask = np.isfinite(values).all(axis=-1).all(axis=-1).any(axis=-1)
    if (ts_mask.sum()/float(len(timestamps))) < 0.7:
        raise ValueError('no finite solutions')
    values = values[ts_mask, ...]
    timestamps = timestamps[ts_mask]

    if values.shape[1] > 1:
        #Only use channels selected in h5 file
        values = values[:, katdal_obj.channels, ...]
        for ts_idx in range(values.shape[0]):
            #Interpolate across nans in channel axis.
            for pol_idx in range(values.shape[2]):
                for ant_idx in range(values.shape[3]):
                    values[ts_idx, : , pol_idx, ant_idx] = interpolate_nans_1d(values[ts_idx, :, pol_idx, ant_idx],
                                                                               kind='linear',
                                                                               fill_value='extrapolate',
                                                                               assume_sorted=True,
                                                                               )

    kind = kwargs.get('kind', 'linear') # default if none given
    if np.iscomplexobj(values) and kind not in ['zero', 'nearest']:
        interp = ComplexInterpolate1D
    else:
        interp = scipy.interpolate.interp1d
    return interp(
                  timestamps,
                  values,
                  axis=0,
                  fill_value='extrapolate',
                  assume_sorted=True,
                  **kwargs)

def applycal(katdal_obj):
    """
    Apply the K, B, G solutions to visibilities at provided timestamps.
    Optionally recompute the weights as well.

    Returns
    =======
    katdal_obj: containing vis and weights (optional) with cal solns applied.
    """
    katdal_obj._cal_pol_ordering = _get_cal_pol_ordering(katdal_obj)
    katdal_obj._cal_ant_ordering = _get_cal_antlist(katdal_obj)
    katdal_obj._data_channel_freqs = katdal_obj.channel_freqs
    katdal_obj._delay_to_phase = (-2j * np.pi * katdal_obj._data_channel_freqs)[np.newaxis, :, np.newaxis, np.newaxis]
    katdal_obj._cp_lookup = [[(katdal_obj._cal_ant_ordering.index(prod[0][:-1]), katdal_obj._cal_pol_ordering[prod[0][-1]],),
                        (katdal_obj._cal_ant_ordering.index(prod[1][:-1]), katdal_obj._cal_pol_ordering[prod[1][-1]],)]
                        for prod in katdal_obj.corr_products]

    initcal = lambda kind: {'interp' : None, 'solns' : None, 'kind' : kind}
    katdal_obj._cal_solns = {
                             'K' : initcal('linear'),
                             'B' : initcal('zero'),
                             'G' : initcal('linear'),
                            }
    for key in katdal_obj._cal_solns.keys():
        try:
            katdal_obj._cal_solns[key]['interp'] = _get_cal_product(
                                                                    'cal_product_'+key,
                                                                    katdal_obj,
                                                                    kind = katdal_obj._cal_solns[key]['kind'],
                                                                   )
        except CalibrationReadError:
            raise
        except: # no delay cal solution from telstate
            # should raise a warning
            raise

    def _cal_interp(timestamps):
        # Interpolate the calibration solutions for the selected range
        for key in katdal_obj._cal_solns.keys():
            if katdal_obj._cal_solns[key]['interp'] is not None:
                katdal_obj._cal_solns[key]['solns'] = katdal_obj._cal_solns[key]['interp'](timestamps)

        if katdal_obj._cal_solns['K']['solns'] is not None:
            katdal_obj._cal_solns['K']['solns'] = np.exp(katdal_obj._cal_solns['K']['solns'] * katdal_obj._delay_to_phase)

    def _cal_vis(vis, keep):
        print 'bla', vis.shape
        print katdal_obj.timestamps.shape,
        print katdal_obj.dumps.shape,
        print katdal_obj.channels.shape
        print katdal_obj.dumps
        _cal_interp(katdal_obj.timestamps)
#         _cal_interp(katdal_obj.dumps)
        print 'bla', vis.shape
        if vis.shape[2]!=len(katdal_obj._cp_lookup):
            raise ValueError('Shape mismatch between correlation products.')
        if vis.shape[1]!=len(katdal_obj._data_channel_freqs):
            raise ValueError('Shape mismatch in frequency axis.')
        if vis.shape[0]!=len(katdal_obj.timestamps):
            raise ValueError('Shape mismatch in timestamps.')

        # calibrate visibilities
        for idx, cp in enumerate(katdal_obj._cp_lookup):
            _cal_shape = vis[:, :, idx].shape
            dummy = np.zeros(_cal_shape)
            default = np.ones(_cal_shape)

            caldata = np.empty(len(katdal_obj._cal_solns.keys()), dtype=object)
            # apply cal solutions in sequence: K, B, G
            for seq, caltype in enumerate(['K', 'B', 'G']):
                X = katdal_obj._cal_solns[caltype]['solns']
                if X is None:
                    print ('TelescopeState does not have %s calibration solutions' % seq)
                    scale = default
                else:
                    scale = dummy + X[:, :, cp[0][1], cp[0][0]] * X[:, :, cp[1][1], cp[1][0]].conj()
                caldata[seq] = scale
            caldata = np.array([(x,) for x in caldata]).squeeze()
            caldata = np.rollaxis(caldata,0,3)
            # ((X*K)/B)/G
            vis[:, :, idx] = (((vis[:, :, idx] * caldata[..., 0]) / caldata[..., 1]) * np.reciprocal(caldata[..., 2])).astype(np.complex64)
        return vis
    katdal_obj.cal_vis = LazyTransform('cal_vis', _cal_vis, dtype = np.complex64)


    def _cal_weights(weights, keep):
        _cal_interp(katdal_obj.timestamps)
        for idx, cp in enumerate(applycal._cp_lookup):
            # scale weights when appling cal solutions in sequence: B, G
            for seq, caltype in enumerate(['B', 'G']):
                X = applycal._cal_solns[caltype]['solns']
                if X is not None:
                    scale = X[:, :, cp[0][1], cp[0][0]] * X[:, :, cp[1][1], cp[1][0]].conj()
                    weights[:,:,idx] *= scale.real**2 + scale.imag**2
        return weights
    katdal_obj.cal_weights = LazyTransform('cal_weights', _cal_weights, dtype = np.complex64)

    return katdal_obj


# -fin-
