#!/usr/bin/env python

import numpy as np
from scipy.interpolate import splprep,splev,BSpline,griddata,bisplev,interp1d,interp2d
from salt3.util.synphot import synphot
from sncosmo.salt2utils import SALT2ColorLaw
import time
from itertools import starmap
from salt3.training import init_hsiao
from sncosmo.models import StretchSource
from scipy.optimize import minimize
from scipy.stats import norm
from scipy.ndimage import gaussian_filter1d
import pylab as plt
from scipy.special import factorial
from astropy.cosmology import Planck15 as cosmo
from sncosmo.constants import HC_ERG_AA

_SCALE_FACTOR = 1e-12

class fitting:
	def __init__(self,n_components,n_colorpars,n_phaseknots,n_waveknots,datadict):

		self.n_phaseknots = n_phaseknots
		self.n_waveknots = n_waveknots
		self.n_components = n_components
		self.n_colorpars = n_colorpars
		self.datadict = datadict
		
	def mcmc(self,saltfitter,guess,
			 n_processes,n_mcmc_steps,
			 n_burnin_mcmc):

		saltfitter.debug = False
		
		x,phase,wave,M0,M0err,M1,M1err,cov_M0_M1,\
			modelerr,clpars,clerr,clscat,SNParams = \
			saltfitter.mcmcfit(guess,n_mcmc_steps,n_burnin_mcmc)
		return x,phase,wave,M0,M0err,M1,M1err,cov_M0_M1,\
			modelerr,clpars,clerr,clscat,SNParams,'Adaptive MCMC was successful'

class loglike:
	def __init__(self,guess,datadict,parlist,days_interp=5,**kwargs):

		assert type(parlist) == np.ndarray

		self.nstep = 0
		self.parlist = parlist
		self.datadict = datadict
		self.guess = guess
		self.days_interp = days_interp
		
		for key, value in kwargs.items(): 
			self.__dict__[key] = value

		# pre-set some indices
		self.m0min = np.min(np.where(self.parlist == 'm0')[0])
		self.m0max = np.max(np.where(self.parlist == 'm0')[0])
		self.errmin = np.min(np.where(self.parlist == 'modelerr')[0])
		self.errmax = np.max(np.where(self.parlist == 'modelerr')[0])

		# set some phase/wavelength arrays
		self.splinecolorwave = np.linspace(self.colorwaverange[0],self.colorwaverange[1],self.n_colorpars)
		self.phasebins = np.linspace(self.phaserange[0]-self.days_interp,self.phaserange[1]+self.days_interp,
							 1+ (self.phaserange[1]-self.phaserange[0]+2*self.days_interp)/self.phaseres)
		self.wavebins = np.linspace(self.waverange[0],self.waverange[1],
							 1+(self.waverange[1]-self.waverange[0])/self.waveres)

		self.phase = np.linspace(self.phaserange[0]-self.days_interp,self.phaserange[1]+self.days_interp,
								 (self.phaserange[1]-self.phaserange[0]+2*self.days_interp)/self.phaseoutres,False)
		self.wave = np.linspace(self.waverange[0],self.waverange[1],(self.waverange[1]-self.waverange[0])/self.waveoutres,False)

		self.hsiaoflux = init_hsiao.get_hsiao(hsiaofile=self.initmodelfile,Bfilt=self.initbfilt,
											  phaserange=self.phaserange,waverange=self.waverange,
											  phaseinterpres=self.phaseoutres,waveinterpres=self.waveoutres,
											  phasesplineres=self.phaseres,wavesplineres=self.waveres,
											  days_interp=self.days_interp)
		
		self.neff=0
		self.updateEffectivePoints(guess)

		# initialize the model
		self.components = self.SALTModel(guess)

		int1d = interp1d(self.phase,self.components[0],axis=0)
		self.m0guess = -19.36

		# set up the filters
		self.stdmag = {}
		for survey in self.kcordict.keys():
			if survey == 'default': 
				self.stdmag[survey] = {}
				self.stdmag[survey]['B']=synphot(
					self.kcordict[survey]['primarywave'],self.kcordict[survey]['AB'],
					filtwave=self.kcordict['default']['Bwave'],filttp=self.kcordict[survey]['Btp'],
					zpoff=0)
				continue
			self.stdmag[survey] = {}
			primarywave = self.kcordict[survey]['primarywave']
			for flt in self.kcordict[survey].keys():
				if flt == 'filtwave' or flt == 'primarywave' or flt == 'snflux' or flt == 'AB' or flt == 'BD17': continue
				if self.kcordict[survey][flt]['magsys'] == 'AB': primarykey = 'AB'
				elif self.kcordict[survey][flt]['magsys'] == 'Vega': primarykey = 'Vega'
				elif self.kcordict[survey][flt]['magsys'] == 'BD17': primarykey = 'BD17'
				self.stdmag[survey][flt] = synphot(
					primarywave,self.kcordict[survey][primarykey],
					filtwave=self.kcordict[survey]['filtwave'],
					filttp=self.kcordict[survey][flt]['filttrans'],
					zpoff=0) - self.kcordict[survey][flt]['primarymag']


		#Count number of photometric and spectroscopic points
		self.num_spec=0
		self.num_phot=0
		for sn in self.datadict.keys():
			photdata = self.datadict[sn]['photdata']
			specdata = self.datadict[sn]['specdata']
			survey = self.datadict[sn]['survey']
			filtwave = self.kcordict[survey]['filtwave']
			z = self.datadict[sn]['zHelio']
			
			self.num_spec+=len(specdata.keys())
			
			for flt in np.unique(photdata['filt']):
				# synthetic photometry
				filtwave = self.kcordict[survey]['filtwave']
				filttrans = self.kcordict[survey][flt]['filttrans']
			
				#Check how much mass of the filter is inside the wavelength range
				filtRange=(filtwave/(1+z)>self.wavebins.min()) &(filtwave/(1+z) <self.wavebins.max())
				num = np.trapz((filttrans*filtwave/(1+z))[filtRange],filtwave[filtRange]/(1+z))
				denom = np.trapz(filttrans*filtwave/(1+z),filtwave/(1+z))
				if not num/denom < 1-self.filter_mass_tolerance:
					self.num_phot+=1

	def maxlikefit(self,x,pool=None,debug=False):
		"""
		Calculates the likelihood of given SALT model to photometric and spectroscopic data given during initialization
		
		Parameters
		----------
		x : array
			SALT model parameters
			
		pool :	multiprocessing.pool.Pool, optional
			Optional worker pool to be used for calculating chi2 values for each SN. If not provided, all work is done in root process
		
		debug : boolean, optional
			Debug flag
		
		Returns
		-------
		
		chi2: float
			Goodness of fit of model to training data	
		"""
		
		#Set up SALT model
		components = self.SALTModel(x)
		salterr = self.ErrModel(x)
		if self.n_components == 1: M0 = components[0]
		elif self.n_components == 2: M0,M1 = components
		if self.n_colorpars:
			colorLaw = SALT2ColorLaw(self.colorwaverange, x[self.parlist == 'cl'])
		else: colorLaw = None; colorScat = None

		chi2 = 0
		#Construct arguments for maxlikeforSN method
		args=[(sn,x,components,salterr,colorLaw,colorScat,debug) for sn in self.datadict.keys()]
		#If worker pool available, use it to calculate chi2 for each SN; otherwise, do it in this process
		if pool:
			loglike=sum(pool.starmap(self.loglikeforSN,args))
		else:
			loglike=sum(starmap(self.loglikeforSN,args))
					
		loglike -= self.regularizationChi2(x,self.regulargradientphase,self.regulargradientwave,self.regulardyad)
		logp = loglike + self.m0prior(components)

		self.nstep += 1
		
		print(logp*-2)
		return logp

	def m0prior(self,components):

		int1d = interp1d(self.phase,components[0],axis=0)
		m0B = synphot(self.wave,int1d(0),filtwave=self.kcordict['default']['Bwave'],
					  filttp=self.kcordict['default']['Btp'])-self.stdmag['default']['B']
		logprior = norm.logpdf(m0B,self.m0guess,0.02)

		return logprior

	def EBVprior(self,x):
		pass
		
	def loglikeforSN(self,sn,x,components=None,salterr=None,
					 colorLaw=None,colorScat=None,
					 debug=False):
		"""
		Calculates the likelihood of given SALT model to photometric and spectroscopic observations of a single SN 
		
		Parameters
		----------
		sn : str
			Name of supernova to compare to model
			
		x : array
			SALT model parameters
			
		components: array_like, optional
			SALT model components, if not provided will be derived from SALT model parameters passed in \'x\'
		
		colorLaw: function, optional
			SALT color law which takes wavelength as an argument

		debug : boolean, optional
			Debug flag
		
		Returns
		-------
		chi2: float
			Model chi2 relative to training data	
		"""
		x = np.array(x)
		
		#Set up SALT model
		if components is None:
			components = self.SALTModel(x)
		if salterr is None:
			salterr = self.ErrModel(x)
		if self.n_components == 1: M0 = components[0]
		elif self.n_components == 2: M0,M1 = components
		
		#Declare variables
		photdata = self.datadict[sn]['photdata']
		specdata = self.datadict[sn]['specdata']
		survey = self.datadict[sn]['survey']
		filtwave = self.kcordict[survey]['filtwave']
		z = self.datadict[sn]['zHelio']
		obswave = self.wave*(1+z)
		obsphase = self.phase*(1+z)
		
		x0,x1,c,tpkoff = \
			x[self.parlist == 'x0_%s'%sn][0],x[self.parlist == 'x1_%s'%sn][0],\
			x[self.parlist == 'c_%s'%sn][0],x[self.parlist == 'tpkoff_%s'%sn][0]
			
		#Calculate spectral model
		if self.n_components == 1:
			saltflux = x0*M0
		elif self.n_components == 2:
			saltflux = x0*(M0 + x1*M1)
			#saltflux_simple = x0*M0/_SCALE_FACTOR
		if colorLaw:
			saltflux *= 10. ** (-0.4 * colorLaw(self.wave) * c)
		saltflux *= _SCALE_FACTOR
			
		loglike = 0
		int1d = interp1d(obsphase,saltflux,axis=0)
		interr1d = interp1d(obsphase,salterr,axis=0)
		for k in specdata.keys():
			phase=specdata[k]['tobs']+tpkoff
			if phase < obsphase.min() or phase > obsphase.max(): raise RuntimeError('Phase {} is out of extrapolated phase range for SN {} with tpkoff {}'.format(phase,sn,tpkoff))
			saltfluxinterp = int1d(phase)
			#Interpolate SALT flux at observed wavelengths and multiply by recalibration factor
			coeffs=x[self.parlist=='specrecal_{}_{}'.format(sn,k)]
			coeffs/=factorial(np.arange(len(coeffs)))
			saltfluxinterp2 = np.interp(specdata[k]['wavelength'],obswave,saltfluxinterp)*np.exp(np.poly1d(coeffs)((specdata[k]['wavelength']-np.mean(specdata[k]['wavelength']))/self.specrange_wavescale_specrecal))
			print(np.mean(saltfluxinterp2),np.mean(specdata[k]['flux']))
			loglike -= np.sum((saltfluxinterp2-specdata[k]['flux'])**2./specdata[k]['fluxerr']**2.)*self.num_phot/self.num_spec/2.
			
		for flt in np.unique(photdata['filt']):
			# check if filter 
			filttrans = self.kcordict[survey][flt]['filttrans']

			g = (obswave >= filtwave[0]) & (obswave <= filtwave[-1])  # overlap range
			
			
			pbspl = np.interp(obswave[g],filtwave,filttrans)
			pbspl *= obswave[g]
				
			denom = np.trapz(pbspl,obswave[g])

			phase=photdata['tobs']+tpkoff
			#Select data from the appropriate filter filter
			selectFilter=(photdata['filt']==flt)
			if ((phase<obsphase.min()) | (phase>obsphase.max())).any():
				raise RuntimeError('Phases {} are out of extrapolated phase range for SN {} with tpkoff {}'.format(phase[((phase<self.phase.min()) | (phase>self.phase.max()))],sn,tpkoff))
			filtPhot={key:photdata[key][selectFilter] for key in photdata}
			phase=phase[selectFilter]
			try:
				#Array output indices match time along 0th axis, wavelength along 1st axis
				saltfluxinterp = int1d(phase)
				salterrinterp = interr1d(phase)
			except:
				import pdb; pdb.set_trace()
			# synthetic photometry from SALT model
			# Integrate along wavelength axis
			modelsynflux=np.trapz(pbspl[np.newaxis,:]*saltfluxinterp[:,g]/HC_ERG_AA,obswave[g],axis=1)/denom

			modelflux = modelsynflux*10**(-0.4*self.kcordict[survey][flt]['zpoff'])*10**(0.4*(self.stdmag[survey][flt]+27.5))
			modelerr=np.trapz(pbspl[np.newaxis,:]*salterrinterp[:,g],obswave[g],axis=1)/denom

			if colorScat: colorerr = splev(self.kcordict[survey][flt]['lambdaeff'],
										   (self.splinecolorwave,x[self.parlist == 'clscat'],3))
			else: colorerr = 0.0
			

			# likelihood function
			loglike += (-(filtPhot['fluxcal']-modelflux)**2./2./(filtPhot['fluxcalerr']**2. + modelflux**2.*modelerr**2. + colorerr**2.)+\
						np.log(1/(np.sqrt(2*np.pi)*np.sqrt(filtPhot['fluxcalerr']**2. + modelflux**2.*modelerr**2. + colorerr**2.)))).sum()
			#loglike -= ((filtPhot['fluxcal']-modelflux)**2./2./(filtPhot['fluxcalerr']**2. + modelflux**2.*0.01**2. + colorerr**2.)-\
			#		 np.log(1/(np.sqrt(2*np.pi)*np.sqrt(filtPhot['fluxcalerr']**2. + modelflux**2.*0.01**2. + colorerr**2.)))).sum()
			if self.debug and self.nstep > 7000:
				if self.nstep > 1500 and flt == 'd' and sn == 5999398:
					print(sn)
					import pylab as plt
					plt.ion()
					plt.clf()
					plt.errorbar(filtPhot['tobs'],modelflux,fmt='o',color='C0',label='model')
					plt.errorbar(filtPhot['tobs'],filtPhot['fluxcal'],yerr=filtPhot['fluxcalerr'],fmt='o',color='C1',label='obs')
					import pdb; pdb.set_trace()

		return loglike

		
	def specchi2(self):

		return chi2
	
	def SALTModel(self,x,bsorder=3,evaluatePhase=None,evaluateWave=None):

		try: m0pars = x[self.m0min:self.m0max]
		except: import pdb; pdb.set_trace()
		m0 = bisplev(self.phase if evaluatePhase is None else evaluatePhase,
					 self.wave if evaluateWave is None else evaluateWave,
					 (self.phaseknotloc,self.waveknotloc,m0pars,bsorder,bsorder))

		if self.n_components == 2:
			m1pars = x[self.parlist == 'm1']
			m1 = bisplev(self.phase if evaluatePhase is None else evaluatePhase,
						 self.wave if evaluateWave is None else evaluateWave,
						 (self.phaseknotloc,self.waveknotloc,m1pars,bsorder,bsorder))
			components = (m0,m1)
		elif self.n_components == 1:
			components = (m0,)
		else:
			raise RuntimeError('A maximum of two principal components is allowed')
			
		return components

	def ErrModel(self,x,bsorder=3,evaluatePhase=None,evaluateWave=None):

		try: errpars = x[self.errmin:self.errmax]
		except: import pdb; pdb.set_trace()

		modelerr = bisplev(self.phase if evaluatePhase is None else evaluatePhase,
						   self.wave if evaluateWave is None else evaluateWave,
						   (self.errphaseknotloc,self.errwaveknotloc,errpars,bsorder,bsorder))
		
		return modelerr

	def getPars(self,loglikes,x,nburn=500,bsorder=3,mkplots=False):

		axcount = 0; parcount = 0
		from matplotlib.backends.backend_pdf import PdfPages
		pdf_pages = PdfPages('output/MCMC_hist.pdf')
		fig = plt.figure()
		
		m0pars = np.array([])
		m0err = np.array([])
		for i in np.where(self.parlist == 'm0')[0]:
			m0pars = np.append(m0pars,x[i][nburn:].mean())#/_SCALE_FACTOR)
			m0err = np.append(m0err,x[i][nburn:].std())#/_SCALE_FACTOR)
			if mkplots:
				if not parcount % 9:
					subnum = axcount%9+1
					ax = plt.subplot(3,3,subnum)
					axcount += 1
					md,std = np.mean(x[i][nburn:]),np.std(x[i][nburn:])
					histbins = np.linspace(md-3*std,md+3*std,50)
					ax.hist(x[i][nburn:],bins=histbins)
					ax.set_title('M0')
					if axcount % 9 == 8:
						pdf_pages.savefig(fig)
						fig = plt.figure()
				parcount += 1

		m1pars = np.array([])
		m1err = np.array([])
		parcount = 0
		for i in np.where(self.parlist == 'm1')[0]:
			m1pars = np.append(m1pars,x[i][nburn:].mean())#/_SCALE_FACTOR)
			m1err = np.append(m1err,x[i][nburn:].std())#/_SCALE_FACTOR)
			if mkplots:
				if not parcount % 9:
					subnum = axcount%9+1
					ax = plt.subplot(3,3,subnum)
					axcount += 1
					md,std = np.mean(x[i][nburn:]),np.std(x[i][nburn:])
					histbins = np.linspace(md-3*std,md+3*std,50)
					ax.hist(x[i][nburn:],bins=histbins)
					ax.set_title('M1')
					if axcount % 9 == 8:
						pdf_pages.savefig(fig)
						fig = plt.figure()
				parcount += 1

		# covmat (diagonals only?)
		m0_m1_cov = np.zeros(len(m0pars))
		chain_len = len(m0pars)
		iM0 = self.parlist == 'm0'
		iM1 = self.parlist == 'm1'
		m0mean = np.repeat(x[iM0][:,nburn:].mean(axis=1),np.shape(x[iM0][:,nburn:])[1]).reshape(np.shape(x[iM0][:,nburn:]))
		m1mean = np.repeat(x[iM1][:,nburn:].mean(axis=1),np.shape(x[iM1][:,nburn:])[1]).reshape(np.shape(x[iM1][:,nburn:]))
		m0var = x[iM0][:,nburn:]-m0mean
		m1var = x[iM1][:,nburn:]-m1mean

		for i in range(len(m0pars)):
			for j in range(len(m1pars)):
				if i == j: m0_m1_cov[i] = np.sum(m0var[j]*m1var[i])
		m0_m1_cov /= chain_len


		modelerrpars = np.array([])
		modelerrerr = np.array([])
		for i in np.where(self.parlist == 'modelerr')[0]:
			modelerrpars = np.append(modelerrpars,x[i][nburn:].mean())
			modelerrerr = np.append(modelerrerr,x[i][nburn:].std())

		clpars = np.array([])
		clerr = np.array([])
		for i in np.where(self.parlist == 'cl')[0]:
			clpars = np.append(clpars,x[i][nburn:].mean())
			clerr = np.append(clpars,x[i][nburn:].std())

		clscatpars = np.array([])
		clscaterr = np.array([])
		for i in np.where(self.parlist == 'clscat')[0]:
			clscatpars = np.append(clpars,x[i][nburn:].mean())
			clscaterr = np.append(clpars,x[i][nburn:].std())


		result=np.mean(x,axis=1)
		resultsdict = {}
		n_sn = len(self.datadict.keys())
		for k in self.datadict.keys():
			tpk_init = self.datadict[k]['photdata']['mjd'][0] - self.datadict[k]['photdata']['tobs'][0]
			resultsdict[k] = {'x0':x[self.parlist == 'x0_%s'%k][0][nburn:].mean(),
							  'x1':x[self.parlist == 'x1_%s'%k][0][nburn:].mean(),
							  'c':x[self.parlist == 'x1_%s'%k][0][nburn:].mean(),
							  'tpkoff':x[self.parlist == 'tpkoff_%s'%k][0][nburn:].mean(),
							  'x0err':x[self.parlist == 'x0_%s'%k][0][nburn:].std(),
							  'x1err':x[self.parlist == 'x1_%s'%k][0][nburn:].std(),
							  'cerr':x[self.parlist == 'x1_%s'%k][0][nburn:].std(),
							  'tpkofferr':x[self.parlist == 'tpkoff_%s'%k][0][nburn:].std()}

		m0 = bisplev(self.phase,self.wave,(self.phaseknotloc,self.waveknotloc,m0pars,bsorder,bsorder))
		m0err = bisplev(self.phase,self.wave,(self.phaseknotloc,self.waveknotloc,m0err,bsorder,bsorder))
		if len(m1pars):
			m1 = bisplev(self.phase,self.wave,(self.phaseknotloc,self.waveknotloc,m1pars,bsorder,bsorder))
			m1err = bisplev(self.phase,self.wave,(self.phaseknotloc,self.waveknotloc,m1err,bsorder,bsorder))
		else:
			m1 = np.zeros(np.shape(m0))
			m1err = np.zeros(np.shape(m0))

	
		cov_m0_m1 = bisplev(self.phase,self.wave,(self.phaseknotloc,self.waveknotloc,m0_m1_cov,bsorder,bsorder))
		modelerr = bisplev(self.phase,self.wave,(self.errphaseknotloc,self.errwaveknotloc,modelerrpars,bsorder,bsorder))
		clscat = splev(self.wave,(self.errwaveknotloc,clscatpars,3))
		if not len(clpars): clpars = []

		if mkplots:
			for snpar in ['x0','x1','c','tpkoff']:
				subnum = axcount%9+1
				ax = plt.subplot(3,3,subnum)
				axcount += 1
				md = np.mean(x[self.parlist == '%s_%s'%(snpar,k)][0][nburn:])
				std = np.std(x[self.parlist == '%s_%s'%(snpar,k)][0][nburn:])
				histbins = np.linspace(md-3*std,md+3*std,50)
				ax.hist(x[self.parlist == '%s_%s'%(snpar,k)][0][nburn:],bins=histbins)
				ax.set_title('%s_%s'%(snpar,k))
				if axcount % 9 == 8:
					pdf_pages.savefig(fig)
					fig = plt.figure()


			pdf_pages.savefig(fig)			
			pdf_pages.close()

		return(result,self.phase,self.wave,m0,m0err,m1,m1err,cov_m0_m1,modelerr,
			   clpars,clerr,clscat,resultsdict)

	
	def synphot(self,sourceflux,zpoff,survey=None,flt=None,redshift=0):
		obswave = self.wave*(1+redshift)

		filtwave = self.kcordict[survey]['filtwave']
		filttrans = self.kcordict[survey][flt]['filttrans']

		g = (obswave >= filtwave[0]) & (obswave <= filtwave[-1])  # overlap range

		pbspl = np.interp(obswave[g],filtwave,filttrans)
		pbspl *= obswave[g]

		res = np.trapz(pbspl*sourceflux[g]/HC_ERG_AA,obswave[g])/np.trapz(pbspl,obswave[g])
		return(zpoff-2.5*np.log10(res))
		
	def updateEffectivePoints(self,x):
		"""
		Updates the "effective number of points" constraining a given bin in phase/wavelength space. Should be called any time tpkoff values are recalculated
		
		Parameters
		----------
			
		x : array
			SALT model parameters
					
		"""
		#Clean out array
		self.neff=np.zeros((self.phasebins.size-1,self.wavebins.size-1))
		for sn in self.datadict.keys():
			tpkoff=x[self.parlist == 'tpkoff_%s'%sn]
			photdata = self.datadict[sn]['photdata']
			specdata = self.datadict[sn]['specdata']
			survey = self.datadict[sn]['survey']
			filtwave = self.kcordict[survey]['filtwave']
			z = self.datadict[sn]['zHelio']
			
			#For each spectrum, add one point to each bin for every spectral measurement in that bin
			for k in specdata.keys():
				restWave=specdata[k]['wavelength']/(1+z)
				restWave=restWave[(restWave>self.wavebins.min())&(restWave<self.wavebins.max())]
				phase=(specdata[k]['tobs']+tpkoff)/(1+z)
				phaseIndex=np.searchsorted(self.phasebins,phase,'left')[0]
				waveIndex=np.searchsorted(self.wavebins,restWave,'left')[0]
				self.neff[phaseIndex][waveIndex]+=1
			#For each photometric filter, weight the contribution by  
			for flt in np.unique(photdata['filt']):
				filttrans = self.kcordict[survey][flt]['filttrans']
				
				g = (self.wavebins[:-1]  >= filtwave[0]/(1+z)) & (self.wavebins[:-1] <= filtwave[-1]/(1+z))  # overlap range
				pbspl = np.zeros(g.sum())
				for i in range(g.sum()):
					j=np.where(g)[0][i]
					pbspl[i]=trapIntegrate(self.wavebins[j],self.wavebins[j+1],filtwave/(1+z),filttrans*filtwave/(1+z))

				#Normalize it so that total number of points added is 1
				pbspl /= np.sum(pbspl)
				#Consider weighting neff by variance for each measurement?
				for phase in (photdata['tobs'][(photdata['filt']==flt)]+tpkoff)/(1+z):
					self.neff[np.searchsorted(self.phasebins,phase),:][g]+=pbspl

		#Smear it out a bit along phase axis
		self.neff=gaussian_filter1d(self.neff,1,0)

		self.neff=np.clip(self.neff,1e-2*self.neff.max(),None)
		self.plotEffectivePoints([-12.5,0,12.5,40],'neff.png')
		self.plotEffectivePoints(None,'neff-heatmap.png')

	def plotEffectivePoints(self,phases=None,output=None):

		import matplotlib.pyplot as plt
		#print(self.neff)
		if phases is None:
			plt.imshow(self.neff,cmap='Greys',aspect='auto')
			xticks=np.linspace(0,self.wavebins.size,8,False)
			plt.xticks(xticks,['{:.0f}'.format(self.wavebins[int(x)]) for x in xticks])
			plt.xlabel('$\lambda$ / Angstrom')
			yticks=np.linspace(0,self.phasebins.size,8,False)
			plt.yticks(yticks,['{:.0f}'.format(self.phasebins[int(x)]) for x in yticks])
			plt.ylabel('Phase / days')
		else:
			inds=np.searchsorted(self.phasebins,phases)
			for i in inds:
				plt.plot(self.wavebins[:-1],self.neff[i,:],label='{:.1f} days'.format(self.phasebins[i]))
			plt.ylabel('$N_eff$')
			plt.xlabel('$\lambda (\AA)$')
			plt.xlim(self.wavebins.min(),self.wavebins.max())
			plt.legend()
		
		if output is None:
			plt.show()
		else:
			plt.savefig(output,dpi=288)
		plt.clf()


	def regularizationChi2(self, x,gradientPhase,gradientWave,dyad):
		
		fluxes=self.SALTModel(x,evaluatePhase=self.phasebins[:-1],evaluateWave=self.wavebins[:-1])

		chi2wavegrad=0
		chi2phasegrad=0
		chi2dyad=0
		for i in range(self.n_components):
			exponent=1
			fluxvals=fluxes[i]/np.mean(fluxes[i])
			if gradientWave !=0:
				chi2wavegrad+= self.waveres**exponent/((self.wavebins.size-1)**2 *(self.phasebins.size-1)) * (( (fluxvals[:,:,np.newaxis]-fluxvals[:,np.newaxis,:])**2   /   (np.sqrt(self.neff[:,:,np.newaxis]*self.neff[:,np.newaxis,:])* np.abs(self.wavebins[np.newaxis,np.newaxis,:-1]-self.wavebins[np.newaxis,:-1,np.newaxis])**exponent))[:,~np.diag(np.ones(self.wavebins.size-1,dtype=bool))]).sum()
			if gradientPhase != 0:
				chi2phasegrad+= self.phaseres**exponent/((self.phasebins.size-1)**2 *(self.wavebins.size-1) ) * ((  (fluxvals[np.newaxis,:,:]-fluxvals[:,np.newaxis,:])**2   /   (np.sqrt(self.neff[np.newaxis,:,:]*self.neff[:,np.newaxis,:])* np.abs(self.phasebins[:-1,np.newaxis,np.newaxis]-self.phasebins[np.newaxis,:-1,np.newaxis])**exponent))[~np.diag(np.ones(self.phasebins.size-1,dtype=bool)),:]).sum()
			if dyad!= 0:
				chi2dyadvals=(   (fluxvals[:,np.newaxis,:,np.newaxis] * fluxvals[np.newaxis,:,np.newaxis,:] - fluxvals[np.newaxis,:,:,np.newaxis] * fluxvals[:,np.newaxis,np.newaxis,:])**2)   /   (np.sqrt(np.sqrt(self.neff[np.newaxis,:,np.newaxis,:]*self.neff[np.newaxis,:,:,np.newaxis]*self.neff[:,np.newaxis,:,np.newaxis]*self.neff[:,np.newaxis,np.newaxis,:]))*np.abs(self.wavebins[np.newaxis,np.newaxis,:-1,np.newaxis]-self.wavebins[np.newaxis,np.newaxis,np.newaxis,:-1])*np.abs(self.phasebins[:-1,np.newaxis,np.newaxis,np.newaxis]-self.phasebins[np.newaxis,:-1,np.newaxis,np.newaxis]))
				chi2dyad+=self.phaseres*self.waveres/( (self.wavebins.size-1) *(self.phasebins.size-1))**2  * chi2dyadvals[~np.isnan(chi2dyadvals)].sum()
        
		return gradientWave*chi2wavegrad+dyad*chi2dyad+gradientPhase*chi2phasegrad


class mcmc(loglike):

	def adjust_model(self,X,stepfactor=1.0,nstep=0):

		stepfactor = 1
		X2 = np.zeros(self.npar)
		for i,par in zip(range(self.npar),self.parlist):
			if par == 'm0':
				m0scalestep = self.stepsize_magscale_M0*stepfactor
				m0addstep = self.stepsize_magadd_M0*stepfactor

				scalefactor = 10**(0.4*np.random.normal(scale=m0scalestep))
				X2[i] = X[i]*scalefactor + self.M0stddev*np.random.normal(scale=self.stepsize_magadd_M0*stepfactor)
			elif par == 'm1':
				m1scalestep = self.stepsize_magscale_M1*stepfactor
				if m1scalestep < 0.001: m1scalestep = 0.001
				m1addstep = self.stepsize_magadd_M1*stepfactor
				if m1addstep < 0.001: m1addstep = 0.001

				scalefactor = 10**(0.4*np.random.normal(scale=m1scalestep))
				X2[i] = X[i]*scalefactor + self.M1stddev*np.random.normal(scale=self.stepsize_magadd_M1*stepfactor)
			elif par == 'cl': X2[i] = X[i]*np.random.normal(scale=self.stepsize_cl*stepfactor)

			elif par.startswith('specrecal'): X2[i] = X[i]+np.random.normal(scale=self.stepsize_specrecal*stepfactor)
			elif par.startswith('x0'): X2[i] = X[i]*10**(0.4*np.random.normal(scale=self.stepsize_x0))
			elif par.startswith('x1'):
				X2[i] = X[i] + np.random.normal(scale=self.stepsize_x1*stepfactor)
			elif par.startswith('c'): X2[i] = X[i] + np.random.normal(scale=self.stepsize_c)#*stepfactor)
			elif par.startswith('tpkoff'):
				X2[i] = X[i] + np.random.normal(scale=self.stepsize_tpk*stepfactor)
			else:
				raise ValueError('Parameter {} has not been assigned a step factor'.format(par))

		return X2

	def get_proposal_cov(self, M2, n, beta=0.05):
		d, _ = M2.shape
		init_period = self.nsteps_before_adaptive
		s_0, s_opt, C_0 = self.AMpars['sigma_0'], self.AMpars['sigma_opt'], self.AMpars['C_0']
		if n<= init_period or np.random.rand()<=beta:
			return C_0, False
		else:
			# We can always divide M2 by n-1 since n > init_period
			return (s_opt/(self.nsteps_adaptive_memory - 1))*M2, True
	
	def generate_AM_candidate(self, current, M2, n):
		prop_cov,adjust_flag = self.get_proposal_cov(M2, n)

		candidate = np.zeros(self.npar)
		if not adjust_flag:
			for i,par in zip(range(self.npar),self.parlist):
				if par == 'm0':
					m0scalestep = self.stepsize_magscale_M0
					m0addstep = self.stepsize_magadd_M0
					scalefactor = 10**(0.4*np.random.normal(scale=m0scalestep))
					candidate[i] = current[i]*scalefactor + np.random.normal(scale=prop_cov[i,i])
				elif par == 'm1':
					m1scalestep = self.stepsize_magscale_M1
					scalefactor = 10**(0.4*np.random.normal(scale=m1scalestep))
					candidate[i] = current[i]*scalefactor + np.random.normal(scale=prop_cov[i,i])
				elif par.startswith('x0'):
					candidate[i] = current[i]*10**(0.4*np.random.normal(scale=self.stepsize_x0))
				elif par.startswith('specrecal'): X2[i] = X[i]+np.random.normal(scale=self.stepsize_specrecal)
				else:
					candidate[i] = current[i] + np.random.normal(0,np.sqrt(prop_cov[i,i]))
		else:
			for i,par in zip(range(self.npar),self.parlist):
				candidate[i] = np.random.normal(loc=current[i],scale=np.sqrt(prop_cov[i,i]))

		return candidate

	def update_moments(self,mean, M2, sample, n):
		next_n = (n + 1)
		w = 1/next_n
		new_mean = mean + w*(sample - mean)
		delta_bf, delta_af = sample - mean, sample - new_mean
		new_M2 = M2 + np.outer(delta_bf, delta_af)

		return new_mean, new_M2

	def get_propcov_init(self,x):
		x
		C_0 = np.zeros([len(x),len(x)])
		for i,par in zip(range(self.npar),self.parlist):
			if par == 'm0':
				C_0[i,i] = (self.M0stddev*self.stepsize_magadd_M0)**2.
			if par == 'modelerr':
				C_0[i,i] = (self.errstddev)**2.
			elif par == 'm1':
				C_0[i,i] = (self.M1stddev*self.stepsize_magadd_M1)**2.
			elif par.startswith('x0'):
				C_0[i,i] = 0.0 
			elif par.startswith('x1'):
				C_0[i,i] = self.stepsize_x1**2.
			elif par.startswith('c'): C_0[i,i] = (self.stepsize_c)**2.
			elif par.startswith('specrecal'): C_0[i,i] = self.stepsize_specrecal**2.
			elif par.startswith('tpkoff'):
				C_0[i,i] = self.stepsize_tpk**2.
				
		self.AMpars = {'C_0':C_0,
					   'sigma_0':0.1/np.sqrt(self.npar),
					   'sigma_opt':2.38*self.adaptive_sigma_opt_scale/np.sqrt(self.npar)}

	
	def mcmcfit(self,x,nsteps,nburn,pool=None,debug=False):
		npar = len(x)
		self.npar = npar
		
		# initial log likelihood
		loglikes = [self.maxlikefit(x,pool=pool,debug=debug)]
		loglike_history = []
		Xlast = x[:]
		self.M0stddev = np.std(Xlast[self.parlist == 'm0'])
		self.M1stddev = np.std(Xlast[self.parlist == 'm1'])
		self.errstddev = self.stepsize_magscale_err
		mean, M2 = x[:], np.zeros([len(x),len(x)])
		mean_recent, M2_recent = x[:], np.zeros([len(x),len(x)])

		self.get_propcov_init(x)

		outpars = [[] for i in range(npar)]
		accept = 0
		nstep = 0
		accept_frac = 0.5
		accept_frac_recent = 0.5
		accepted_history = np.array([])
		n_adaptive = 0
		while nstep < nsteps:
			nstep += 1
			n_adaptive += 1
			
			if not nstep % 50 and nstep > 250:
				accept_frac_recent = len(accepted_history[-100:][accepted_history[-100:] == True])/100.
			
			X = self.generate_AM_candidate(current=Xlast, M2=M2_recent, n=nstep)
			
			# loglike
			this_loglike = self.maxlikefit(X,pool=pool,debug=debug)

			# accepted?
			accept_bool = self.accept(loglikes[-1],this_loglike)
			if accept_bool:
				for j in range(npar):
					outpars[j] += [X[j]]
				loglikes+=[this_loglike]
				loglike_history+=[this_loglike]
				accept += 1
				Xlast = X[:]
				print('step = %i, accepted = %i, acceptance = %.3f, recent acceptance = %.3f'%(
					nstep,accept,accept/float(nstep),accept_frac_recent))
			else:
				for j in range(npar):
					outpars[j] += [Xlast[j]]
				loglike_history += [this_loglike]
			accepted_history = np.append(accepted_history,accept_bool)

			mean, M2 = self.update_moments(mean, M2, Xlast, n_adaptive)
			if not n_adaptive % self.nsteps_adaptive_memory:
				n_adaptive = 0
				M2_recent = M2[:]
				mean_recent = mean[:]
				mean, M2 = Xlast[:], np.zeros([len(x),len(x)])
			
		print('acceptance = %.3f'%(accept/float(nstep)))
		if nstep < nburn:
			raise RuntimeError('Not enough steps to wait 500 before burn-in')
		xfinal,phase,wave,M0,M0err,M1,M1err,cov_M0_M1,\
			modelerr,clpars,clerr,clscat,SNParams = \
			self.getPars(loglike_history,np.array(outpars),nburn=nburn)
		
		return xfinal,phase,wave,M0,M0err,M1,M1err,cov_M0_M1,\
			modelerr,clpars,clerr,clscat,SNParams
		
	def accept(self, last_loglike, this_loglike):
		alpha = np.exp(this_loglike - last_loglike)
		return_bool = False
		if alpha >= 1:
			return_bool = True
		else:
			if np.random.rand() < alpha:
				return_bool = True
		return return_bool
	
def trapIntegrate(a,b,xs,ys):
	if (a<xs.min()) or (b>xs.max()):
		raise ValueError('Bounds of integration outside of provided values')
	aInd,bInd=np.searchsorted(xs,[a,b])
	if aInd==bInd:
		return ((ys[aInd]-ys[aInd-1])/(xs[aInd]-xs[aInd-1])*((a+b)/2-xs[aInd-1])+ys[aInd-1])*(b-a)
	elif aInd+1==bInd:
		return ((ys[aInd]-ys[aInd-1])/(xs[aInd]-xs[aInd-1])*((a+xs[aInd])/2-xs[aInd-1])+ys[aInd-1])*(xs[aInd]-a) + ((ys[bInd]-ys[bInd-1])/(xs[bInd]-xs[bInd-1])*((xs[bInd-1]+b)/2-xs[bInd-1])+ys[bInd-1])*(b-xs[bInd-1])
	else:
		return np.trapz(ys[(xs>=a)&(xs<b)],xs[(xs>=a)&(xs<b)])+((ys[aInd]-ys[aInd-1])/(xs[aInd]-xs[aInd-1])*((a+xs[aInd])/2-xs[aInd-1])+ys[aInd-1])*(xs[aInd]-a) + ((ys[bInd]-ys[bInd-1])/(xs[bInd]-xs[bInd-1])*((xs[bInd-1]+b)/2-xs[bInd-1])+ys[bInd-1])*(b-xs[bInd-1])
		
