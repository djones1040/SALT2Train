#!/usr/bin/env python
# D. Jones - 11/1/2019
from scipy.interpolate import interp1d
from scipy.integrate import trapz
import numpy as np
from salt3.util.synphot import synphot
from sncosmo.constants import HC_ERG_AA

def getScaleForSN(spectrum,photdata,kcordict,survey):

	dwave = np.median(spectrum['wavelength'][1:]-spectrum['wavelength'][:-1])
	primarywave = kcordict[survey]['primarywave']
	filtwave = kcordict[survey]['filtwave']
	maxwave = np.max(spectrum['wavelength'])
	minwave = np.min(spectrum['wavelength'])
	
	scale_guess = []
	scale_guess_err = []
	flt_out = []
	for flt in np.unique(photdata['filt']):
		# preliminaries
		if kcordict[survey][flt]['magsys'] == 'AB': primarykey = 'AB'
		elif kcordict[survey][flt]['magsys'].upper() == 'VEGA': primarykey = 'Vega'
		elif kcordict[survey][flt]['magsys'] == 'BD17': primarykey = 'BD17'
		stdmag = synphot(
			primarywave,kcordict[survey][primarykey],
			filtwave=kcordict[survey]['filtwave'],
			filttp=kcordict[survey][flt]['filttrans'],
			zpoff=0) - kcordict[survey][flt]['primarymag']
		fluxfactor = 10**(0.4*(stdmag+27.5))
		wht = np.sum(kcordict[survey][flt]['filttrans'][
			(kcordict[survey]['filtwave'] > maxwave) |
			(kcordict[survey]['filtwave'] < minwave)])/np.sum(kcordict[survey][flt]['filttrans'])
		if wht > 0.02: continue
		flt_out += [flt]

		filttrans = kcordict[survey][flt]['filttrans']
		pbspl = np.interp(spectrum['wavelength'],filtwave,filttrans)
		denom = np.trapz(pbspl,spectrum['wavelength'])

		pbspl = np.interp(spectrum['wavelength'],filtwave,filttrans)
		pbspl *= spectrum['wavelength']
		denom = trapz(pbspl,spectrum['wavelength'])
		pbspl /= denom*HC_ERG_AA

		
		phot1d = interp1d(photdata['mjd'][photdata['filt'] == flt],
						  photdata['fluxcal'][photdata['filt'] == flt],
						  axis=0,kind='linear',bounds_error=True,
						  assume_sorted=True)
		photerr1d = interp1d(photdata['mjd'][photdata['filt'] == flt],
							 photdata['fluxcalerr'][photdata['filt'] == flt],
							 axis=0,kind='linear',bounds_error=True,
							 assume_sorted=True)

		try:
			flux = phot1d(spectrum['mjd'])
			fluxerr = photerr1d(spectrum['mjd'])
		except: continue
		
		synph = np.sum(spectrum['flux']*pbspl)*dwave*fluxfactor
		synpherr = np.sqrt(np.sum((spectrum['fluxerr']*pbspl)**2.))*dwave*fluxfactor
		scale_guess += [flux/synph]
		scale_guess_err += [scale_guess[-1]*np.sqrt((synpherr/synph)**2. + (fluxerr/flux)**2.)]

	if len(scale_guess):
		scale_guess,scale_guess_err = np.array(scale_guess),np.array(scale_guess_err)
		scale_out = np.average(scale_guess,weights=1/scale_guess_err**2.)
		print(scale_out)
		return np.log(1/scale_out)
	else:
		return 1
