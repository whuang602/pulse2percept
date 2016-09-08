# -*effectivecurrent2brightness -*-
"""effectivecurrent2brightness
This transforms the effective current into brightness for a single point in
space based on the Horsager model as modified by Devyani
Inputs: a vector of effective current over time
Output: a vector of brightness over time
"""
from __future__ import print_function
import numpy as np
from scipy.misc import factorial
from scipy.signal import fftconvolve
from scipy.signal import convolve2d
from scipy.special import expit

import electrode2currentmap as e2cm
import utils
from utils import TimeSeries


class TemporalModel(object):
    def __init__(self, tsample=.005/1000, tau1=.42/1000, tau2=45.25/1000,
                 tau3=26.25/1000, epsilon=8.73, asymptote=14, slope=3,
                 shift=16):
        """
        A model of temporal integration from retina pixels

        Fs : sampling rate

        tau1 = .42/1000  is a parameter for the fast leaky integrator, from
        Alan model, tends to be between .24 - .65

        tau2 = 45.25/1000  integrator for charge accumulation, has values
        between 38-57

        epsilon = scaling factor for the effects of charge accumulation 2-3 for
        threshold or 8-10 for suprathreshold. If using the Krishnan model then e is 0.1118

        tau3 = 26.25/1000

        parameters for a stationary nonlinearity providing a continuous
        function that nonlinearly rescales the response based on Nanduri et al
        2012, equation 3:

        asymptote = 14
        slope =3
        shift =16
        """
        self.tsample = tsample
        self.tau1 = tau1
        self.tau2 = tau2
        self.tau3 = tau3
        self.epsilon = epsilon
        self.asymptote = asymptote
        self.slope = slope
        self.shift = shift

        self._setup()

    def _setup(self):
        """Performs one-time setup calculations.

        Gamma functions used as convolution kernels do not depend on input
        data, hence can be calculated once, then re-used (trade off memory for
        speed).
        """
        # gamma1 is used to calculate the fast response
        t = np.arange(0, 20 * self.tau1, self.tsample)
        self.gamma1 = e2cm.gamma(1, self.tau1, t)

        # gamma2 is used to calculate charge accumulation
        t = np.arange(0, 8 * self.tau2, self.tsample)
        self.gamma2 = e2cm.gamma(1, self.tau2, t)

        # gamma3 is used to calculate the slow response
        t = np.arange(0, 8 * self.tau3, self.tsample)
        self.gamma3 = e2cm.gamma(3, self.tau3, t)


    def _fast_response(self, b1, dojit=True):
        """Fast response function (Box 2) of the perceptual sensitivity model.

        Convolve a stimulus `b1` with a temporal low-pass filter (1-stage gamma)
        with time constant self.tau1.
        This is Box 2 in Nanduri et al. (2012).

        Parameters
        ----------
        b1 : TimeSeries
           Temporal signal to process, b1(r,t) in Nanduri et al. (2012).
        dojit : bool, optional
           If True (default), use numba just-in-time compilation.

        Returns
        -------
        b2 : array
           Fast response, b2(r,t) in Nanduri et al. (2012).

        Notes
        -----
        The function utils.sparseconv can be much faster than np.convolve and
        scipy.signals.fftconvolve if `b1` is sparse and much longer than the
        convolution kernel.

        The gamma function is pre-calculated for speed-up.

        The output is not converted to a TimeSeries object for speed-up.

        """
        return self.tsample * utils.sparseconv(self.gamma1,
                                               b1.data,
                                               mode='same',
                                               dojit=dojit)

    def _charge_accumulation(self, fr, ecm):
        ca = self.tsample * np.cumsum(np.maximum(0, ecm.data), axis=-1)
        charge_acc = self.epsilon * self.tsample * fftconvolve(ca,
                                                               self.gamma2,
                                                               mode='same')
        return np.maximum(0, fr - charge_acc)

    def _stationary_nonlinearity(self, b3):
        """Stationary nonlinearity of the perceptual sensitivity model (Box 4)

        Nonlinearly rescale a temporal signal `b3` across space and time, based
        on a sigmoidal function dependent on the maximum value of `b3`.
        This is Box 4 in Nanduri et al. (2012).

        The parameter values of the asymptote, slope, and shift of the logistic
        function are given by self.asymptote, self.slope, and self.shift,
        respectively.

        Parameters
        ----------
        b3 : array
           Temporal signal to process, b3(r,t) in Nanduri et al. (2012).

        Returns
        -------
        b4 : array
           Rescaled signal, b4(r,t) in Nanduri et al. (2012).

        Notes
        -----
        The expit (logistic) function is used for speed-up.

        The output is not converted to a TimeSeries object for speed-up.

        """
        scale = expit((b3.max() - self.shift) / self.slope) * self.asymptote
        return b3 / b3.max() * scale

    def _slow_response(self, b4):
        """Slow response function (Box 5) of the perceptual sensitivity model.

        Convolve a stimulus `b4` with a low-pass filter (3-stage gamma)
        with time constant self.tau3.
        This is Box 5 in Nanduri et al. (2012).

        Parameters
        ----------
        b4 : array
           Temporal signal to process, b4(r,t) in Nanduri et al. (2012)

        Returns
        -------
        b5 : array
           Slow response, b5(r,t) in Nanduri et al. (2012).

        Notes
        -----
        This is by far the most computationally involved part of the perceptual
        sensitivity model.

        The gamma kernel needs to be cropped as tightly as possible for speed
        sake. fftconvolve already takes care of optimal kernel/data size.

        The output is not converted to a TimeSeries object for speed-up.

        """
        return self.tsample * fftconvolve(b4, self.gamma3, mode='same')

    def model_cascade(self, ecm, dojit):
        # FIXME need to make sure ecm.tsample == self.tsample
        modelver = 'Nanduri'

        resp = self._fast_response(ecm, dojit=dojit)
        if modelver == 'Nanduri':
            resp = self._charge_accumulation(resp, ecm)

        resp = self._stationary_nonlinearity(resp)
        resp = self._slow_response(resp)
        return TimeSeries(self.tsample, resp)


def pulse2percept(temporal_model, ecs, retina, stimuli, rs, dojit=True, n_jobs=-1, tol=.05):
    """
    From pulses (stimuli) to percepts (spatio-temporal)

    Parameters
    ----------
    temporal_model : temporalModel class instance.
    ecs : ndarray
    retina : a Retina class instance.
    stimuli : list
    subsample_factor : float/int, optional
    dojit : bool, optional
    """

    ecs_list = []
    idx_list = []
    for xx in range(retina.gridx.shape[1]):
        for yy in range(retina.gridx.shape[0]):
            if np.all(ecs[yy, xx] < tol):
                pass
            else:
                ecs_list.append(ecs[yy, xx])
                idx_list.append([yy, xx])
                # the current contributed by each electrode for that spatial
                # location

    # pulse train for each electrode
    stim_data = np.array([s.data for s in stimuli])
    sr_list = utils.parfor(calc_pixel, ecs_list, n_jobs=n_jobs,
                           func_args=[stim_data, temporal_model,
                                      rs,  stimuli[0].tsample, dojit])
    bm = np.zeros(retina.gridx.shape + (sr_list[0].data.shape[-1], ))
    idxer = tuple(np.array(idx_list)[:, i] for i in range(2))
    bm[idxer] = [sr.data for sr in sr_list]
    return TimeSeries(sr_list[0].tsample, bm)


def calc_pixel(ecs_vector, stim_data, temporal_model, resample_factor,
               tsample, dojit='False'):
    ecm = e2cm.ecm(ecs_vector, stim_data, tsample)
    sr = temporal_model.model_cascade(ecm, dojit=dojit)
    sr.resample(resample_factor)
    return sr

def onoffFiltering(movie, n, sig=[.1, .25],amp=[.01, -0.005]):
    """
    From a movie to a version that is filtered by a collection on and off cells
    of sizes

    Parameters
    ----------
    movie: movie to be filtered
    n : the sizes of the retinal ganglion cells (in μm, 293 μm equals 1 degree)
    """
    onmovie = np.zeros([movie.data.shape[0], movie.data.shape[1], movie.data.shape[2]])
    offmovie = np.zeros([movie.data.shape[0], movie.data.shape[1], movie.data.shape[2]])
    newfiltImgOn=np.zeros([movie.shape[0], movie.shape[1]])
    newfiltImgOff=np.zeros([movie.shape[0], movie.shape[1]])
    pad = max(n)*2
    for xx in range(movie.shape[-1]):
        oldimg=movie[:, :, xx].data
        tmpimg=np.mean(np.mean(oldimg))*np.ones([oldimg.shape[0]+pad*2,oldimg.shape[1]+pad*2])
        img = insertImg(tmpimg, oldimg)
        filtImgOn=np.zeros([img.shape[0], img.shape[1]])
        filtImgOff=np.zeros([img.shape[0], img.shape[1]])
        
        for i in range(n.shape[0]): 
            [x,y] = np.meshgrid(np.linspace(-1,1,n[i]),np.linspace(-1,1,n[i]))   
            rsq = x**2+y**2
            dx = x[0,1]-x[0,0]    
            on = np.exp(-rsq/(2*sig[0]**2))*(dx**2)/(2*np.pi*sig[0]**2)
            off = np.exp(-rsq/(2*sig[1]**2))*(dx**2)/(2*np.pi*sig[1]**2)
            filt = on-off
            tmp_on = convolve2d(img,filt,'same')/n.shape[-1]
            tmp_off=tmp_on
            tmp_on= np.where(tmp_on>0, tmp_on, 0) 
            tmp_off= -np.where(tmp_off<0, tmp_off, 0)
             #   rectified = np.where(ptrain.data > 0, ptrain.data, 0)
            filtImgOn =    filtImgOn+tmp_on/n.shape[0] 
            filtImgOff =   filtImgOff+tmp_off/n.shape[0] 

        # Remove padding
        nopad=np.zeros([img.shape[0]-pad*2, img.shape[1]-pad*2])
        newfiltImgOn[:,:] = insertImg(nopad,filtImgOn)
        newfiltImgOff[:, :] = insertImg(nopad,filtImgOff)
        onmovie[:, :, xx]=newfiltImgOn
        offmovie[:, :, xx]=newfiltImgOff
        
    return (onmovie, offmovie)

def onoffRecombine(onmovie, offmovie):
    """
    From a movie as filtered by on and off cells, 
    to a recombined version that is either based on an electronic 
    prosthetic (on + off) or recombined as might be done by a cortical
    cell in normal vision (on-off) 
    Parameters
    ----------
    movie: on and off movies to be recombined
    combination : options are 'both' returns both prosthetic and normal vision, 'normal' and 'prosthetic'
    """  

    prostheticmovie=onmovie + offmovie
    normalmovie=onmovie - offmovie
    return (normalmovie, prostheticmovie)


def insertImg(out_img,in_img): 
    """ insertImg(out_img,in_img)
    Inserts in_img into the center of out_img.  
    if in_img is larger than out_img, in_img is cropped and centered.
    """

    if in_img.shape[0]>out_img.shape[0]:
        x0 = np.floor([(in_img.shape[0]-out_img.shape[0])/2])
        xend=x0+out_img.shape[0]    
        in_img=in_img[x0:xend, :]
       
    if in_img.shape[1]>out_img.shape[1]:
        y0 = np.floor([(in_img.shape[1]-out_img.shape[1])/2])   
        yend=y0+out_img.shape[1]
        in_img=in_img[:, y0:yend]
       
    x0 = np.floor([(out_img.shape[0]-in_img.shape[0])/2])
    y0 = np.floor([(out_img.shape[1]-in_img.shape[1])/2])
    out_img[x0:x0+in_img.shape[0], y0:y0+in_img.shape[1]] = in_img
    
    return out_img
