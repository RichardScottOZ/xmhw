#!/usr/bin/env python
# coding: utf-8
# Copyright 2020 ARC Centre of Excellence for Climate Extremes
# author: Paola Petrelli <paola.petrelli@utas.edu.au>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import xarray as xr
import numpy as np
import pandas as pd
import dask
import sys
import time
from .identify import (join_gaps, define_events, runavg, dask_percentile, window_roll,
                      land_check, feb29, add_doy, annotate_ds) 
from .features import flip_cold
from .exception import XmhwException


def threshold(temp, tdim='time', climatologyPeriod=[None,None], pctile=90, windowHalfWidth=5, smoothPercentile=True, 
                   smoothPercentileWidth=31, maxPadLength=False, coldSpells=False, Ly=False):
    """Calculate threshold and seasonal climatology (varying with day-of-year)

    Inputs:

      temp    Temperature array

    Outputs:
        'thresh'               Seasonally varying threshold (e.g., 90th percentile)
        'seas'                 Climatological seasonal cycle
        'missing'              A vector of TRUE/FALSE indicating which elements in 
                               temp were missing values for the MHWs detection

    Options:

      tdim                   String: time dimension name, default='time'
      climatologyPeriod      List of integers: period over which climatology is calculated, specified
                             as list of start and end years. Default is to calculate
                             over the full range of years in the supplied time series.
                             Alternate periods suppled as a list e.g. [1983,2012].
      pctile                 Integer: threshold percentile (%) for detection of extreme values
                             (DEFAULT = 90)
      windowHalfWidth        Integer: width of window (one sided) about day-of-year used for
                             the pooling of values and calculation of threshold percentile
                             (DEFAULT = 5 [days])
      smoothPercentile       Boolean: switch indicating whether to smooth the threshold
                             percentile timeseries with a moving average (DEFAULT = True)
      smoothPercentileWidth  Integer: width of moving average window for smoothing threshold
                             (DEFAULT = 31 [days], should be odd number)
      maxPadLength           Integer: specifies the maximum length [days] over which to interpolate
                             (pad) missing data (specified as nans) in input temp time series.
                             i.e., any consecutive blocks of NaNs with length greater
                             than maxPadLength will be left as NaN.
                             (DEFAULT = None, interpolates over all missing values).
      coldSpells             Boolean: specifies if the code should detect cold events instead of
                             heat events. (DEFAULT = False)
      Ly                     Boolean: specifies if the length of the year is < 365/366 days (e.g. a 
                             360 day year from a climate model). This affects the calculation
                             of the climatology. (DEFAULT = False)
    """

    # check smooth percentile window width is odd
    if smoothPercentileWidth%2 == 0:
        raise XmhwException("smoothPercentileWidth should be odd")

    # Set climatology period, if unset use full range of available data
    if all(climatologyPeriod):
        tslice = {tdim: slice(f'{climatologyPeriod[0]}-01-01', f'{climatologyPeriod[1]}-12-31')}
        temp = temp.sel(**tslice)
    # save original attributes in a dictionary to be assigned to final datset
    ds_attrs = {}
    ds_attrs['ts'] = temp.attrs
    #ds_attrs[tdim+'encoding'] = temp.encoding
    for c in temp.coords:
        ds_attrs[c] = temp[c].attrs
    # return an array stacked on all dimensions excluded time
    # Land cells are removed
    # new dimensions are (time,cell)
    ts = land_check(temp, tdim=tdim)
    ts = add_doy(ts, tdim=tdim)

    # Flip ts time series if detecting cold spells
    if coldSpells:
        ts = -1.*ts

    # Pad missing values for all consecutive missing blocks of length <= maxPadLength
    # In Eric this happens regardless??
    if maxPadLength:
        ts = pad(ts, maxPadLength=maxPadLength)

    # Apply window_roll to each cell.
    # Window_roll first finds for each day of the year all the ts values that falls in
    # a window +/-windowHalfWidth, then concatenates them in a new timeseries
    # create dataset so we can preserve all dimensions
    #ds = xr.Dataset()
    #ds['ts'] = ts
    #ts = ts.chunk({tdim: -1, 'cell':1})
    #ds = ds.chunk({tdim: -1, 'cell':1})
    climls = []
    for c in ts.cell:
        climls.append( calc_thresh(ts.sel(cell=c), windowHalfWidth,
                       pctile, smoothPercentile, smoothPercentileWidth,
                       Ly, tdim) )
    results =dask.compute(climls)
    ds = xr.concat(results[0], dim=ts.cell)
    ds = ds.unstack('cell')
    ds = annotate_ds(ds, ds_attrs, 'clim')
    # add all parameters used to global attributes 
    params = f"""Threshold calculated using:
    {pctile} percentile;
    climatology period is {ts[0,0][tdim].dt.year.values}-{ts[-1,0][tdim].dt.year.values}'; 
    window half width used for percentile is {windowHalfWidth}"""
    if smoothPercentile:
        params = params + f";  width of moving average window to smooth percentile is {smoothPercentileWidth}"
    ds.attrs['xmhw_parameters'] = params 
    return ds



@dask.delayed(nout=1)
def calc_thresh(ts, windowHalfWidth, pctile, smoothPercentile,
                smoothPercentileWidth, Ly, tdim):
    """ Calculate threshold for one cell grid at the time
    """
    twindow = window_roll(ts, windowHalfWidth, tdim)

    # rechunk twindow otherwise it is passed to dask_percentile as a numpy array 
    twindow = twindow.chunk({'z': -1})
    
     # Calculate threshold and seasonal climatology across years
    thresh_climYear = (twindow
                       .groupby('doy')
                       .reduce(dask_percentile, dim='z', q=pctile)).compute()
    seas_climYear = (twindow
                       .groupby('doy')
                       .reduce(np.nanmean)).compute()

    # calculate value for 29 Feb from mean of 28-29 feb and 1 Mar
    # add this is done only if calendar include 29Feb 
    thresh_climYear.loc[dict(doy=60)] = feb29(thresh_climYear)
    seas_climYear.loc[dict(doy=60)] = feb29(seas_climYear)
    # Smooth if desired
    if smoothPercentile:
        # If the length of year is < 365/366 (e.g. a 360 day year from a Climate Model)
        if Ly:
            valid = ~np.isnan(thresh_climYear)
        # >= 365-day year
        else:
            valid =  np.ones(len(thresh_climYear), dtype=bool)
        thresh_climYear[valid] = runavg(thresh_climYear[valid], smoothPercentileWidth)
        seas_climYear[valid] = runavg(seas_climYear[valid], smoothPercentileWidth)
  # fix name of arrays
    thresh_climYear.name = 'threshold'
    seas_climYear.name = 'seasonal'
    # Save vector indicating which points in temp are missing values
    #missing = np.isnan(ts)
    # Set all remaining missing temp values equal to the climatology
    #seas_climYear = xr.where(missing, ts, seas_climYear)

    # Save in dataset
    ds = xr.Dataset() 
    ds['thresh'] = thresh_climYear
    ds['seas'] = seas_climYear
    #ds['missing'] = missing
    return ds


def detect(temp, th, se, minDuration=5, joinAcrossGaps=True, maxGap=2, maxPadLength=None, coldSpells=False, tdim='time', intermediate=False): 
    """

    Applies the Hobday et al. (2016) marine heat wave definition to an input time
    series of temp ('temp') along with a time vector ('t'). Outputs properties of
    all detected marine heat waves.

    Inputs:

      temp    Temperature array [1D  xarray of length T]
      clim    Climatology of SST. Each key (following list) is a seasonally-varying
              time series [1D numpy array of length T] of a particular measure:

        'th'               Seasonally varying threshold (e.g., 90th percentile)
        'se'                 Climatological seasonal cycle
        'missing'              A vector of TRUE/FALSE indicating which elements in 
                               temp were missing values for the MHWs detection

      
    
    Outputs:

      mhw     Detected marine heat waves (MHWs). Each key (following list) is a
              list of length N where N is the number of detected MHWs:
              ....
      ds      stacked dataset with sst and climatologies along time axis - Optional only if intermediate is True


    Options:

      minDuration            Integer: minimum duration for acceptance detected MHWs
                             (DEFAULT = 5 [days])
      joinAcrossGaps         Boolean: switch indicating whether to join MHWs      
                             which occur before/after a short gap (DEFAULT = True)
      maxGap                 Maximum length of gap allowed for the joining of MHWs
                             (DEFAULT = 2 [days])
      maxPadLength           Integer: specifies the maximum length [days] over which to interpolate
                             (pad) missing data (specified as nans) in input temp time series.
                             i.e., any consecutive blocks of NaNs with length greater
                             than maxPadLength will be left as NaN.
                             (DEFAULT = None, interpolates over all missing values, boolean).
      coldSpells             Boolean: specifies if the code should detect cold events instead of
                             heat events. (DEFAULT = False)
      tdim                   String: name of time dimension. (DEFAULT='time')
      intermediate           Boolean: if True also output stacked dataset with sst and climatologies along time axis. (default: False)
    """
  
   
    
    # check maxGap < minDuration 
    if maxGap >= minDuration:
        raise XmhwException("Maximum gap between mhw events should be smaller than event minimum duration")

    # save original attributes in a dictionary to be assigned to final dataset
    ds_attrs = {}
    ds_attrs['ts'] = temp.attrs
    #ds_attrs[tdim+'encoding'] = temp.encoding
    for c in temp.coords:
        ds_attrs[c] = temp[c].attrs

    # return an array stacked on all dimensions excluding time
    # Land cells are removed
    # new dimensions are (time, cell)
    ts = land_check(temp)
    th = land_check(th, tdim='doy')
    se = land_check(se, tdim='doy')
    # assign doy 
    ts = add_doy(ts)
    # reindex climatologies along time axis
    thresh = th.sel(doy=ts.doy)
    seas = se.sel(doy=ts.doy)

    # Pad missing values for all consecutive missing blocks of length <= maxPadLength
    # maybe this should be done regardless?
    if maxPadLength:
        ts = pad(ts, maxPadLength=maxPadLength)
    # Flip temp time series if detecting cold spells
    if coldSpells:
        ts = -1.*ts

    # Find MHWs as exceedances above the threshold
    #

    # Time series of "True" when threshold is exceeded, "False" otherwise
    bthresh = ts > thresh
    bthresh.name = 'bthresh'
    # join timeseries arrays in dataset to pass to map_blocks
    # so data can be split by chunks
    ds = xr.Dataset({'ts': ts, 'seas': seas, 'thresh': thresh, 'bthresh': bthresh})
    ds = ds.reset_coords(drop=True)
    ds = ds.chunk(chunks={tdim: -1, 'cell': 1})
    # Build a pandas series with the positional indexes as values
    # [0,1,2,3,4,5,6,7,8,9,10,..]
    idxarr = pd.Series(data=np.arange(len(ds[tdim])), index=ds.time.values)
    mhwls = []
    for c in ds.cell:
        mhwls.append(  define_events(ds.sel(cell=c), idxarr,
                     minDuration, joinAcrossGaps, maxGap, intermediate))
    results = dask.compute(mhwls)
    mhw_results = [r[0] for r in results[0]]
    mhw = xr.concat(mhw_results, dim=ds.cell).unstack('cell')
    if intermediate:
        inter_results = [r[1] for r in results[0]]
        mhw_inter = xr.concat(inter_results, dim=ds.cell).unstack('cell')
        mhw_inter = mhw_inter.rename({'index': 'time'})
        mhw_inter = mhw_inter.squeeze(drop=True)
    #del mhw_results, inter_results 
    # if point dimension was added in land_check remove
    mhw = mhw.squeeze(drop=True)

    # Flip climatology and intensities in case of cold spell detection
    if coldSpells:
        mhw = flip_cold(mhw)
    
    mhw  = annotate_ds(mhw, ds_attrs, 'mhw')
    # add all parameters used to global attributes 
    params = f"""MHW detected using:
    {minDuration} days of minimum duration;
        where original timeseries had missing values interpolation was used to fill gaps;"""
    if  maxPadLength:
        params = params + f"; if gaps were more than {maxPadLength} days long, they were left as NaNs"
    if coldSpells:
        params = params + f"; cold events were detected instead of heat events"
    if joinAcrossGaps:
        params = params + f";  events separated by {maxGap} or less days were joined"
    mhw.attrs['xmhw_parameters'] = params 
    if intermediate:
        mhw_inter.squeeze(drop=True)
        return mhw, mhw_inter 
    return mhw 

