from salt3.util.synphot import synphot
from salt3.training import init_hsiao
from salt3.training.priors import SALTPriors

from sncosmo.models import StretchSource
from sncosmo.salt2utils import SALT2ColorLaw
from sncosmo.constants import HC_ERG_AA, MODEL_BANDFLUX_SPACING
from sncosmo.utils import integration_grid

import scipy.stats as ss
from scipy.optimize import minimize, least_squares
from scipy.stats import norm
from scipy.ndimage import gaussian_filter1d
from scipy.special import factorial
from scipy.interpolate import splprep,splev,bisplev,bisplrep,interp1d,interp2d,RegularGridInterpolator,RectBivariateSpline
from scipy.integrate import trapz
from scipy import linalg 

import numpy as np
from numpy.random import standard_normal
from numpy.linalg import slogdet

from astropy.cosmology import Planck15 as cosmo
from multiprocessing import Pool, get_context
from inspect import signature
from functools import partial
from itertools import starmap

import time
import pylab as plt
import extinction
import copy
import warnings
import pyParz

_SCALE_FACTOR = 1e-12
_B_LAMBDA_EFF = np.array([4302.57])	 # B-band-ish wavelength
_V_LAMBDA_EFF = np.array([5428.55])	 # V-band-ish wavelength
warnings.simplefilter('ignore',category=FutureWarning)
	

class SALTResids:
	def __init__(self,guess,datadict,parlist,**kwargs):
		
		assert type(parlist) == np.ndarray
		self.blah = False
		self.debug = False
		self.nstep = 0
		self.parlist = parlist
		self.npar = len(parlist)
		self.datadict = datadict
		
		self.bsorder=3
		self.guess = guess
		self.nsn = len(self.datadict.keys())
		
		for key, value in kwargs.items(): 
			self.__dict__[key] = value

		self.usePriors = []
		self.priorWidths = []
		self.boundedParams = []
		self.bounds = []

		for opt in self.__dict__.keys():
			if opt.startswith('prior_'):
				self.usePriors += [opt[len('prior_'):]]
				self.priorWidths += [self.__dict__[opt]]
			elif opt.startswith('bound_'):
				self.boundedParams += [opt[len('bound_'):]]
				self.bounds += [tuple([float(x) for x in self.__dict__[opt]])]
				
			#self.usePriors=[x.split('prior_')[-1] for x in ] #self.usePriors.split(',')
			#self.priorWidths=[float(x) for x in self.priorWidths.split(',')]

		# pre-set some indices
		self.set_param_indices()
		
		# set some phase/wavelength arrays
		self.phase = np.linspace(self.phaserange[0],self.phaserange[1],
								 int((self.phaserange[1]-self.phaserange[0])/self.phaseoutres)+1,True)

		self.interpMethod='nearest'
		
		nwaveout=int((self.waverange[1]-self.waverange[0])/self.waveoutres)
		self.wave = np.linspace(self.waverange[0],self.waverange[1],
								nwaveout+1,True)
		self.maxPhase=np.where(abs(self.phase) == np.min(abs(self.phase)))[0]
				
		self.neff=0

		# initialize the model
		self.components = self.SALTModel(guess)
		self.salterr = self.ErrModel(guess)
		
		self.m0guess = -19.49 #10**(-0.4*(-19.49-27.5))
		self.extrapolateDecline=0.015
		# set up the filters
		self.stdmag = {}
		self.fluxfactor = {}
		for survey in self.kcordict.keys():
			if survey == 'default': 
				self.stdmag[survey] = {}
				self.bbandoverlap = (self.wave>=self.kcordict['default']['Bwave'].min())&(self.wave<=self.kcordict['default']['Bwave'].max())
				self.bbandpbspl = np.interp(self.wave[self.bbandoverlap],self.kcordict['default']['Bwave'],self.kcordict['default']['Bwave'])
				self.bbandpbspl *= self.wave[self.bbandoverlap]
				self.bbandpbspl /= np.trapz(self.bbandpbspl,self.wave[self.bbandoverlap])*HC_ERG_AA
				self.stdmag[survey]['B']=synphot(
					self.kcordict[survey]['primarywave'],self.kcordict[survey]['AB'],
					filtwave=self.kcordict['default']['Bwave'],filttp=self.kcordict[survey]['Btp'],
					zpoff=0)
				self.kcordict['default']['minlam'] = np.min(self.kcordict['default']['Bwave'][self.kcordict['default']['Btp'] > 0.01])
				self.kcordict['default']['maxlam'] = np.max(self.kcordict['default']['Bwave'][self.kcordict['default']['Btp'] > 0.01])
				self.kcordict['default']['fluxfactor'] = 10**(0.4*(self.stdmag[survey]['B']+27.5))
				continue

			self.stdmag[survey] = {}
			self.fluxfactor[survey] = {}
			primarywave = self.kcordict[survey]['primarywave']
			for flt in self.kcordict[survey].keys():
				if flt == 'filtwave' or flt == 'primarywave' or flt == 'snflux' or flt == 'AB' or flt == 'BD17' or flt == 'Vega': continue
				if self.kcordict[survey][flt]['magsys'] == 'AB': primarykey = 'AB'
				elif self.kcordict[survey][flt]['magsys'].upper() == 'VEGA': primarykey = 'Vega'
				elif self.kcordict[survey][flt]['magsys'] == 'BD17': primarykey = 'BD17'
				self.stdmag[survey][flt] = synphot(
					primarywave,self.kcordict[survey][primarykey],
					filtwave=self.kcordict[survey]['filtwave'],
					filttp=self.kcordict[survey][flt]['filttrans'],
					zpoff=0) - self.kcordict[survey][flt]['primarymag']
				self.fluxfactor[survey][flt] = 10**(0.4*(self.stdmag[survey][flt]+27.5))
				self.kcordict[survey][flt]['minlam'] = np.min(self.kcordict[survey]['filtwave'][self.kcordict[survey][flt]['filttrans'] > 0.01])
				self.kcordict[survey][flt]['maxlam'] = np.max(self.kcordict[survey]['filtwave'][self.kcordict[survey][flt]['filttrans'] > 0.01])
				
		#Count number of photometric and spectroscopic points
		
		self.num_spec=0
		self.num_phot=0
		for sn in self.datadict.keys():
			photdata = self.datadict[sn]['photdata']
			specdata = self.datadict[sn]['specdata']
			survey = self.datadict[sn]['survey']
			filtwave = self.kcordict[survey]['filtwave']
			z = self.datadict[sn]['zHelio']
			self.num_spec += sum([specdata[key]['flux'].size for key in specdata])

			for flt in np.unique(photdata['filt']):
				self.num_phot+=(photdata['filt']==flt).sum()
			#While we're at it, calculate the extinction curve for the milky way
			self.datadict[sn]['mwextcurve']   = 10**(-0.4*extinction.fitzpatrick99(self.wave*(1+z),self.datadict[sn]['MWEBV']*3.1))
			self.datadict[sn]['mwextcurveint'] = interp1d(self.wave*(1+z),self.datadict[sn]['mwextcurve'] ,kind=self.interpMethod,bounds_error=False,fill_value=0,assume_sorted=True)

		self.fixedUncertainties={}
		starttime=time.time()
		#Store derivatives of a spline with fixed knot locations with respect to each knot value
		self.spline_derivs = np.zeros([len(self.phase),len(self.wave),self.im0.size])
		for i in range(self.im0.size):
			self.spline_derivs[:,:,i]=bisplev(self.phase,self.wave,(self.phaseknotloc,self.waveknotloc,np.arange(self.im0.size)==i,self.bsorder,self.bsorder))
		nonzero=np.nonzero(self.spline_derivs)
		self.spline_deriv_interp= RegularGridInterpolator((self.phase,self.wave),self.spline_derivs,self.interpMethod,False,0)
		
		#Repeat for the error model parameters
		self.errorspline_deriv= np.zeros([len(self.phase),len(self.wave),self.imodelerr.size//self.n_components])
		for i in range(self.imodelerr.size//self.n_components):
			self.errorspline_deriv[:,:,i]=bisplev(self.phase, self.wave ,(self.errphaseknotloc,self.errwaveknotloc,np.arange(self.imodelerr.size//self.n_components)==i,self.bsorder,self.bsorder))
		self.errorspline_deriv_interp= RegularGridInterpolator((self.phase,self.wave),self.errorspline_deriv,self.interpMethod,False,0)
		
		#Store the lower and upper edges of the phase/wavelength basis functions
		self.phaseBins=self.phaseknotloc[:-(self.bsorder+1)],self.phaseknotloc[(self.bsorder+1):]
		self.waveBins=self.waveknotloc[:-(self.bsorder+1)],self.waveknotloc[(self.bsorder+1):]
		
		#Find the iqr of the phase/wavelength basis functions
		self.phaseRegularizationPoints=np.zeros(self.phaseBins[0].size*2)
		self.waveRegularizationPoints=np.zeros(self.waveBins[0].size*2)
		binRange=1/3,2/3
		phaseCumulative=(np.cumsum(self.spline_derivs[:,:,::self.waveBins[0].size],axis=0).sum(axis=1)/np.sum(self.spline_derivs[:,:,::self.waveBins[0].size],axis=(0,1)))
		self.phaseRegularizationPoints[::2]=self.phase[ np.abs(phaseCumulative-binRange[0]).argmin(axis=0)]
		self.phaseRegularizationPoints[1::2]=self.phase[ np.abs(phaseCumulative-binRange[1]).argmin(axis=0)]
		
		waveCumulative=(np.cumsum(self.spline_derivs[:,:,:self.waveBins[0].size],axis=1).sum(axis=0)/np.sum(self.spline_derivs[:,:,:self.waveBins[0].size],axis=(0,1)))
		self.waveRegularizationPoints[::2]=self.wave[ np.abs(waveCumulative-binRange[0]).argmin(axis=0)]
		self.waveRegularizationPoints[1::2]=self.wave[ np.abs(waveCumulative-binRange[1]).argmin(axis=0)]

		self.phaseBinCenters=np.array([(self.phase[:,np.newaxis]* self.spline_derivs[:,:,i*(self.waveBins[0].size)]).sum()/self.spline_derivs[:,:,i*(self.waveBins[0].size)].sum() for i in range(self.phaseBins[0].size) ])
		self.waveBinCenters=np.array([(self.wave[np.newaxis,:]* self.spline_derivs[:,:,i]).sum()/self.spline_derivs[:,:,i].sum() for i in range(self.waveBins[0].size)])

		#Find the basis functions evaluated at the centers of the basis functions for use in the regularization derivatives
		self.regularizationDerivs=[np.zeros((self.phaseRegularizationPoints.size,self.waveRegularizationPoints.size,self.im0.size)) for i in range(4)]
		for i in range(len(self.im0)):
			for j,derivs in enumerate([(0,0),(1,0),(0,1),(1,1)]):
				self.regularizationDerivs[j][:,:,i]=bisplev(self.phaseRegularizationPoints,self.waveRegularizationPoints,(self.phaseknotloc,self.waveknotloc,np.arange(self.im0.size)==i,self.bsorder,self.bsorder),dx=derivs[0],dy=derivs[1])

		phase=self.phaseRegularizationPoints
		wave=self.waveRegularizationPoints
		fluxes=self.SALTModel(guess,evaluatePhase=self.phaseRegularizationPoints,evaluateWave=self.waveRegularizationPoints)
		self.guessScale=[np.sqrt(np.mean(f**2)) for f in fluxes]

		self.colorLawDeriv=np.empty((self.wave.size,self.n_colorpars))
		for i in range(self.n_colorpars):
			self.colorLawDeriv[:,i]=SALT2ColorLaw(self.colorwaverange, np.arange(self.n_colorpars)==i)(self.wave)-SALT2ColorLaw(self.colorwaverange, np.zeros(self.n_colorpars))(self.wave)
		self.colorLawDerivInterp=interp1d(self.wave,self.colorLawDeriv,axis=0,kind=self.interpMethod,bounds_error=True,assume_sorted=True)
		
		
		print('Time to calculate spline_derivs: %.2f'%(time.time()-starttime))
		
		
		self.getobswave()
		if self.regularize:
			self.updateEffectivePoints(guess)

		self.priors = SALTPriors(self)
		self.numPriorResids=self.priors.numPriorResids
		
		self.__specFixedUncertainty__={}
		self.__photFixedUncertainty__={}
		self.__components_time_stamp__ = time.time()

	def set_param_indices(self):

		self.parameters = ['x0','x1','c','m0','m1','spcrcl','spcrcl_norm','spcrcl_poly',
						   'modelerr','modelcorr','clscat','clscat_0','clscat_poly']
		self.corrcombinations=sum([[(i,j) for j in range(i+1,self.n_components)]for i in range(self.n_components)] ,[])
		self.m0min = np.min(np.where(self.parlist == 'm0')[0])
		self.m0max = np.max(np.where(self.parlist == 'm0')[0])
		self.errmin = tuple([np.min(np.where(self.parlist == 'modelerr_{}'.format(i))[0]) for i in range(self.n_components)]) 
		self.errmax = tuple([np.max(np.where(self.parlist == 'modelerr_{}'.format(i))[0]) for i in range(self.n_components)]) 
		self.corrmin = tuple([np.min(np.where(self.parlist == 'modelcorr_{}{}'.format(i,j))[0]) for i,j in self.corrcombinations]) 
		self.corrmax = tuple([np.max(np.where(self.parlist == 'modelcorr_{}{}'.format(i,j))[0]) for i,j in self.corrcombinations]) 
		self.im0 = np.where(self.parlist == 'm0')[0]
		self.im1 = np.where(self.parlist == 'm1')[0]
		self.iCL = np.where(self.parlist == 'cl')[0]
		self.ix1 = np.array([i for i, si in enumerate(self.parlist) if si.startswith('x1')])
		self.ix0 = np.array([i for i, si in enumerate(self.parlist) if si.startswith('x0')])
		self.ic	 = np.array([i for i, si in enumerate(self.parlist) if si.startswith('c_')])
		self.itpk = np.array([i for i, si in enumerate(self.parlist) if si.startswith('tpkoff')])
		self.ispcrcl = np.array([i for i, si in enumerate(self.parlist) if si.startswith('specrecal')])
		self.imodelerr = np.array([i for i, si in enumerate(self.parlist) if si.startswith('modelerr')])
		self.imodelcorr = np.array([i for i, si in enumerate(self.parlist) if si.startswith('modelcorr')])
		self.iclscat = np.where(self.parlist=='clscat')[0]
		self.ispcrcl_norm,self.ispcrcl_poly = np.array([],dtype='int'),np.array([],dtype='int')
		if len(self.ispcrcl):
			for i,parname in enumerate(np.unique(self.parlist[self.ispcrcl])):
				self.ispcrcl_norm = np.append(self.ispcrcl_norm,np.where(self.parlist == parname)[0][-1])
				self.ispcrcl_poly = np.append(self.ispcrcl_poly,np.where(self.parlist == parname)[0][:-1])
		self.iclscat_0,self.iclscat_poly = np.array([],dtype='int'),np.array([],dtype='int')
		if len(self.ispcrcl):
			for i,parname in enumerate(np.unique(self.parlist[self.iclscat])):
				self.iclscat_0 = np.append(self.iclscat_0,np.where(self.parlist == parname)[0][-1])
				self.iclscat_poly = np.append(self.iclscat_poly,np.where(self.parlist == parname)[0][:-1])


		
	def getobswave(self):
		"for each filter, setting up some things needed for synthetic photometry"
		
		for sn in self.datadict.keys():
			z = self.datadict[sn]['zHelio']
			survey = self.datadict[sn]['survey']
			filtwave = self.kcordict[survey]['filtwave']
			obswave=self.wave*(1+z)
			self.datadict[sn]['obswave'] = obswave
			
			self.datadict[sn]['obsphase'] = self.phase*(1+z)
			self.datadict[sn]['pbspl'] = {}
			self.datadict[sn]['denom'] = {}
			self.datadict[sn]['idx'] = {}
			self.datadict[sn]['lambdaeff']={}
			self.datadict[sn]['dwave'] = self.wave[1]*(1+z) - self.wave[0]*(1+z)
			for flt in np.unique(self.datadict[sn]['photdata']['filt']):

				filttrans = self.kcordict[survey][flt]['filttrans']

				g = (obswave>= self.kcordict[survey][flt]['minlam']) & (obswave<= self.kcordict[survey][flt]['maxlam'])	# overlap range
				
				self.datadict[sn]['idx'][flt] = g

				pbspl = np.interp(self.datadict[sn]['obswave'][g],filtwave,filttrans)
				pbspl *= self.datadict[sn]['obswave'][g]
				denom = np.trapz(pbspl,self.datadict[sn]['obswave'][g])
				pbspl /= denom*HC_ERG_AA

				self.datadict[sn]['pbspl'][flt] = pbspl[np.newaxis,:]
				self.datadict[sn]['denom'][flt] = denom
				self.datadict[sn]['lambdaeff'][flt] = self.kcordict[survey][flt]['lambdaeff']
		# rest-frame B
		filttrans = self.kcordict['default']['Btp']
		filtwave = self.kcordict['default']['Bwave']
			
		pbspl = np.interp(self.wave,filtwave,filttrans)
		pbspl *= self.wave
		denom = np.trapz(pbspl,self.wave)
		pbspl /= denom*HC_ERG_AA
		self.kcordict['default']['Bpbspl'] = pbspl
		self.kcordict['default']['Bdwave'] = self.wave[1] - self.wave[0]

	def maxlikefit(self,x,storedResults={},varyParams=None,pool=None,debug=False,SpecErrScale=1.0):
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
		if varyParams is None:
			varyParams=np.zeros(self.npar,dtype=bool)
		computeDerivatives=np.any(varyParams)
		#Set up SALT model
		# HACK
		if self.n_colorpars:
			if not 'colorLaw' in storedResults:
				storedResults['colorLaw'] = -0.4 * SALT2ColorLaw(self.colorwaverange, x[self.parlist == 'cl'])(self.wave)
				storedResults['colorLawInterp']= interp1d(self.wave,storedResults['colorLaw'],kind=self.interpMethod,bounds_error=False,fill_value=0,assume_sorted=True)
		else: storedResults['colorLaw'] = 1

		if not all([('specfluxes_{}'.format(sn) in storedResults) and ('photfluxes_{}'.format(sn) in storedResults) for sn in self.datadict]):
				
			if not 'components' in storedResults:
				storedResults['components'] =self.SALTModel(x)
			if not 'componentderivs' in storedResults:
				storedResults['componentderivs'] = self.SALTModelDeriv(x,1,0,self.phase,self.wave)
		
		if not all([('specvariances_{}'.format(sn) in storedResults) and ('photvariances_{}'.format(sn) in storedResults) for sn in self.datadict]):
			storedResults['saltErr']=self.ErrModel(x)
			storedResults['saltCorr']=self.CorrelationModel(x)


		
		# timing stuff
		
		chi2 = 0
		#Construct arguments for maxlikeforSN method
		#If worker pool available, use it to calculate chi2 for each SN; otherwise, do it in this process
		args=[(sn,x,storedResults,varyParams,debug,SpecErrScale) for sn in self.datadict.keys()]
		#args2 = (x,components,componentderivs,salterr,saltcorr,colorLaw,debug,timeit,computeDerivatives,computePCDerivs,SpecErrScale)
		mapFun=pool.map if pool else starmap

		#result = list(pyParz.foreach(self.datadict.keys(),self.loglikeforSN,args2))
		result=list(mapFun(self.loglikeforSN,args))
		if computeDerivatives:
			loglike=sum([r[0] for r in result])
			grad=sum([r[1] for r in result])
		else:
			loglike=sum(result)
		logp = loglike
		if len(self.usePriors):
			priorResids,priorVals,priorJac=self.priors.priorResids(self.usePriors,self.priorWidths,x)	
			logp -=(priorResids**2).sum()/2
			if computeDerivatives:
				grad-= (priorResids [:,np.newaxis] * priorJac).sum(axis=0)
		
		if self.regularize:
			for regularization, weight in [(self.phaseGradientRegularization, self.regulargradientphase),(self.waveGradientRegularization,self.regulargradientwave ),(self.dyadicRegularization,self.regulardyad)]:
				if weight ==0:
					continue
				regResids,regJac=regularization(x,storedResults,varyParams)
				logp-= sum([(res**2).sum()*weight/2 for res in regResids])
				if computeDerivatives:
					for res,jac in zip(regResids,regJac):
						grad -= (res[:,np.newaxis]*jac ).sum(axis=0)
		self.nstep += 1
		print(logp.sum()*-2)

		if computeDerivatives:
			return logp,grad
		else:
			return logp
	
				
	def ResidsForSN(self,x,sn,storedResults,varyParams=None,fixUncertainty=False,SpecErrScale=1.):
		""" This method should be the only one required for any fitter to process the supernova data. 
		Find the residuals of a set of parameters to the photometric and spectroscopic data of a given supernova. 
		Photometric residuals are first decorrelated to diagonalize color scatter"""
		
		if varyParams is None:
			varyParams=np.zeros(self.npar,dtype=bool)
		photmodel,specmodel=self.modelvalsforSN(x,sn,storedResults,varyParams)
		#Separate code for photometry and spectra now, since photometry has to handle a covariance matrix with off-diagonal elements

		if not 'photCholesky_{}'.format(sn) in storedResults:
			#Calculate cholesky matrix for each set of photometric measurements in each filter
			if (photmodel['modelvariance']<0).any():
				warnings.warn('Negative variance in photometry',RuntimeWarning)
				negVals=photmodel['modelvariance']<0
				photmodel['modelvariance'][negVals]=0
			variance=photmodel['fluxvariance']+photmodel['modelvariance']
			Ls,colorvar=[],[]
			#Add color scatter
			for selectFilter,clscat,dclscat in photmodel['colorvariance']:
				flux=clscat*photmodel['modelflux'][selectFilter]
				#Find cholesky matrix as sqrt of diagonal uncertainties, then perform rank one update to incorporate color scatter
				L= np.diag(np.sqrt(variance[selectFilter]))
				for k in range(flux.size):
					r = np.sqrt(L[k,k]**2 + flux[k]**2)
					c = r/L[k,k]
					s = flux[k]/L[k,k]
					L[k,k] = r
					L[k+1:,k] = (L[k+1:,k] + s*flux[k+1:])/c
					flux[k+1:]= c*flux[k+1:] - s*L[k+1:,k]
				Ls+=[L]
				colorvar+=[(selectFilter,clscat,dclscat)]
			storedResults['photCholesky_{}'.format(sn)]=Ls,colorvar

		Ls,colorvar=storedResults['photCholesky_{}'.format(sn)]
		
		photresids={'resid':np.zeros(photmodel['modelflux'].size)}
		photresids['resid_jacobian']=np.zeros((photmodel['modelflux'].size,varyParams.sum()))
		
		if not fixUncertainty: 
			photresids['lognorm']=0
			photresids['lognorm_grad']=np.zeros(self.npar)
		
		for L,(selectFilter,clscat,dclscat) in zip(Ls,colorvar):
			#More stable to solve by backsubstitution than to invert and multiply
			#import pdb;pdb.set_trace()
			fluxdiff=photmodel['modelflux'][selectFilter]-photmodel['dataflux'][selectFilter]
			photresids['resid'][selectFilter]=linalg.solve_triangular(L, fluxdiff,lower=True)

			if not fixUncertainty:
				photresids['lognorm']-= (np.log(np.diag(L)).sum())
		
			if varyParams.any():
				photresids['resid_jacobian'][selectFilter]=linalg.solve_triangular(L,photmodel['modelflux_jacobian'][selectFilter],lower=True)
				if not fixUncertainty:
					varyParlist=self.parlist[varyParams]
					#Cut out zeroed jacobian entries to save time
					nonzero=(~((photmodel['modelvariance_jacobian'][selectFilter]==0) & (photmodel['modelflux_jacobian'][selectFilter]==0)).all(axis=0)) | (self.parlist=='clscat')
					reducedJac=photmodel['modelvariance_jacobian'][selectFilter][:,nonzero]
					
					#Calculate L^-1 (necessary for the diagonal derivative)
					invL=linalg.solve_triangular(L,np.diag(np.ones(fluxdiff.size)),lower=True)
					
					# Calculate the fractional derivative of L w.r.t model parameters
					# L^-1 dL/dx = {L^-1 x d Sigma / dx x L^-T, with the upper triangular part zeroed and the diagonal halved}
					
					#First calculating diagonal part
					fractionalLDeriv=np.dot(invL,np.swapaxes(reducedJac[:,np.newaxis,:]* invL.T[:,:,np.newaxis],0,1))
					
					fluxPrime= linalg.solve_triangular(L,photmodel['modelflux'][selectFilter])
					jacPrime = linalg.solve_triangular(L,photmodel['modelflux_jacobian'][selectFilter][:,nonzero])
					#Derivative w.r.t  color scatter 
					fractionalLDeriv[:,:,varyParlist[nonzero]=='clscat']+= 2*clscat*dclscat[np.newaxis,np.newaxis,:]* np.outer(fluxPrime,fluxPrime)[:,:,np.newaxis]
					#Derivative w.r.t model flux
					dfluxSquared = jacPrime[:,np.newaxis,:]*fluxPrime[np.newaxis,:,np.newaxis]
					#Symmetrize
					dfluxSquared = np.swapaxes(dfluxSquared,0,1)+dfluxSquared
					fractionalLDeriv +=clscat**2 * dfluxSquared
					
					fractionalLDeriv[np.triu(np.ones((fluxdiff.size,fluxdiff.size),dtype=bool),1),:]=0
					fractionalLDeriv[np.diag(np.ones((fluxdiff.size),dtype=bool)),:]/=2
				
					#Multiply by size of transformed residuals and apply to resiudal jacobian
					
					photresids['resid_jacobian'][np.outer(selectFilter,nonzero)]-=np.dot(np.swapaxes(fractionalLDeriv,1,2),photresids['resid'][selectFilter]).flatten()
				
					#Trace of fractional derivative gives the gradient of the lognorm term, since determinant is product of diagonal
					photresids['lognorm_grad'][nonzero]-= np.trace(fractionalLDeriv)
				
		#Handle spectra
		
		variance=specmodel['fluxvariance'] + specmodel['modelvariance']
		if (specmodel['modelvariance']<0).any():
			warnings.warn('Negative variance in spectra',RuntimeWarning)
			negVals=specmodel['modelvariance']<0
			specmodel['modelvariance'][negVals]=0

		uncertainty=np.sqrt(variance)*SpecErrScale
		# HACK
		#uncertainty *= 0.01
		#Suppress the effect of the spectra by multiplying chi^2 by number of photometric points over number of spectral points
		spectralSuppression=np.sqrt(self.num_phot/self.num_spec	)			
		
		specresids={'resid': spectralSuppression * (specmodel['modelflux']-specmodel['dataflux'])/uncertainty}
		specresids['uncertainty'] = uncertainty
		if not fixUncertainty:
			specresids['lognorm']=-np.log((np.sqrt(2*np.pi)*uncertainty)).sum()
		
		specresids['resid_jacobian']=spectralSuppression * specmodel['modelflux_jacobian']/(uncertainty[:,np.newaxis])
		if not fixUncertainty:
			#Calculate jacobian of total spectral uncertainty including reported uncertainties
			uncertainty_jac=  specmodel['modelvariance_jacobian'] / (2*uncertainty[:,np.newaxis])
			specresids['lognorm_grad']= - (uncertainty_jac/uncertainty[:,np.newaxis]).sum(axis=0)
			specresids['resid_jacobian']-=   uncertainty_jac*(specresids['resid'] /uncertainty)[:,np.newaxis]

		return photresids,specresids
		
	def modelvalsforSN(self,x,sn,storedResults,varyParams):		
		
		xDerivs=varyParams[self.parlist=='x0_{}'.format(sn)][0] or varyParams[self.parlist=='x1_{}'.format(sn)][0] 
		tpkDerivs=varyParams[self.parlist=='tpkoff_{}'.format(sn)][0]

		z = self.datadict[sn]['zHelio']
		obsphase = self.datadict[sn]['obsphase'] #self.phase*(1+z)
		x0,x1,c,tpkoff = x[self.parlist == 'x0_%s'%sn],x[self.parlist == 'x1_%s'%sn],\
						 x[self.parlist == 'c_%s'%sn],x[self.parlist == 'tpkoff_%s'%sn]
		colorexp= 10. ** (storedResults['colorLaw'] * c)
		storedResults['colorexp_{}'.format(sn)] =colorexp
		
		calculateFluxes= not ('photfluxes_{}'.format(sn) in storedResults  and  'specfluxes_{}'.format(sn) in storedResults)
		calculateVariances= not ('photvariances_{}'.format(sn) in storedResults and  'specvariances_{}'.format(sn) in storedResults)
		if calculateFluxes:
			#Apply MW extinction
			M0,M1 = storedResults['components']
					
			prefactor=_SCALE_FACTOR/(1+z)*(colorexp*self.datadict[sn]['mwextcurve'][np.newaxis,:])
			if xDerivs:
				
				int1dM0 = interp1d(obsphase,prefactor*M0,axis=0,kind=self.interpMethod,bounds_error=True,assume_sorted=True)
				int1dM1 = interp1d(obsphase,prefactor*M1,axis=0,kind=self.interpMethod,bounds_error=True,assume_sorted=True)
				storedResults['componentsInterp_{}'.format(sn)]=int1dM0,int1dM1
			else:
				mod = x0*prefactor*(M0 + x1*M1)
				storedResults['fluxInterp_{}'.format(sn)] = interp1d(obsphase,mod,axis=0,kind=self.interpMethod,bounds_error=True,assume_sorted=True)
					
			if tpkDerivs and not 'phaseDerivInterp_{}'.format(sn) in storedResults:
				M0phasederiv,M1phasederiv = storedResults['componentderivs']
				phaseDeriv=x0*prefactor*(M0phasederiv +x1*M1phasederiv)
				storedResults['phaseDerivInterp_{}'.format(sn)] = interp1d(obsphase,phaseDeriv,axis=0,kind=self.interpMethod,bounds_error=True,assume_sorted=True)
		
		if calculateVariances:
			prefactor=(self.datadict[sn]['mwextcurve'] *colorexp*  _SCALE_FACTOR/(1+z))
			if xDerivs or varyParams[self.imodelerr].any() or varyParams[self.imodelcorr].any():
				interr1d = [interp1d(obsphase,err * prefactor ,axis=0,kind=self.interpMethod,bounds_error=True,assume_sorted=True) for err in storedResults['saltErr']]
				intcorr1d= [interp1d(obsphase,corr ,axis=0,kind=self.interpMethod,bounds_error=True,assume_sorted=True) for corr in storedResults['saltCorr'] ]
				storedResults['uncertaintyComponentsInterp_{}'.format(sn)]=interr1d
				storedResults['uncertaintyCorrelationsInterp_{}'.format(sn)]=intcorr1d
			else:
				saltErr=storedResults['saltErr']
				saltCorr=storedResults['saltCorr'] 
				modelUncertainty= x0**2 * prefactor**2 *(saltErr[0]**2  + 2*x1* saltCorr[0]*saltErr[0]*saltErr[1] + x1**2 *saltErr[1]**2)
				interr1d=interp1d(obsphase,modelUncertainty ,axis=0,kind=self.interpMethod,bounds_error=True,assume_sorted=True)
				storedResults['modelUncertaintyInterp_{}'.format(sn)]=interr1d
		returndicts=[]
		for valfun,uncertaintyfun,name in [(self.photValsForSN,self.photVarianceForSN,'phot'),(self.specValsForSN,self.specVarianceForSN, 'spec')]:
			fluxkey=name+'fluxes_{}'.format(sn)
			if not fluxkey in storedResults:
				storedResults[fluxkey]=valfun(x,sn, storedResults,varyParams)
			valdict=storedResults[fluxkey]
				
			varkey= name+'variances_{}'.format(sn)
			if not varkey in storedResults:
				storedResults[varkey]=uncertaintyfun(x,sn,storedResults,varyParams)
			uncertaintydict=storedResults[varkey]
			
			returndicts+=[{**valdict ,**uncertaintydict}]
		return returndicts		
	
	def photValsForSN(self,x,sn,storedResults,varyParams):
		z = self.datadict[sn]['zHelio']
		survey = self.datadict[sn]['survey']
		filtwave = self.kcordict[survey]['filtwave']
		obswave = self.datadict[sn]['obswave'] #self.wave*(1+z)
		obsphase = self.datadict[sn]['obsphase'] #self.phase*(1+z)
		photdata = self.datadict[sn]['photdata']
		pbspl = self.datadict[sn]['pbspl']
		dwave = self.datadict[sn]['dwave']
		idx = self.datadict[sn]['idx']
		x0,x1,c,tpkoff = x[self.parlist == 'x0_%s'%sn],x[self.parlist == 'x1_%s'%sn],\
						 x[self.parlist == 'c_%s'%sn],x[self.parlist == 'tpkoff_%s'%sn]
		
		xDeriv=varyParams[self.parlist=='x0_{}'.format(sn)][0] or varyParams[self.parlist=='x1_{}'.format(sn)][0] 
		tpkDeriv=varyParams[self.parlist=='tpkoff_{}'.format(sn)][0]
		cDeriv=varyParams[self.parlist=='c_{}'.format(sn)][0]
		requiredPCDerivs=varyParams[self.im0]|varyParams[self.im1]
		
		varyParList=self.parlist[varyParams]
		
		colorlaw,colorexp=storedResults['colorLaw'],storedResults['colorexp_{}'.format(sn)]
		
		photresultsdict={}
		photresultsdict['modelflux'] = np.zeros(len(photdata['filt']))
		photresultsdict['dataflux'] = photdata['fluxcal']
		photresultsdict['modelflux_jacobian'] = np.zeros((photdata['filt'].size,varyParams.sum()))
		if requiredPCDerivs.all() and not 'pcDeriv_phot_%s'%sn in storedResults:
			summationCache=np.zeros((photdata['filt'].size,self.im0.size))
		for flt in np.unique(photdata['filt']):
			intmult = dwave*self.fluxfactor[survey][flt]*_SCALE_FACTOR/(1+z)*x0
			#Select data from the appropriate filter filter
			selectFilter=(photdata['filt']==flt)
			phase=photdata['tobs']+tpkoff
			phase=phase[selectFilter]
			clippedPhase=np.clip(phase,obsphase.min(),obsphase.max())
			nphase = len(phase)
			#Array output indices match time along 0th axis, wavelength along 1st axis
			if xDeriv:
				int1dM0,int1dM1=storedResults['componentsInterp_{}'.format(sn)]
				M0interp = int1dM0(clippedPhase)
				M1interp = int1dM1(clippedPhase)

				modulatedM0= pbspl[flt]*M0interp[:,idx[flt]]
				modulatedM1=pbspl[flt]*M1interp[:,idx[flt]]
				

				if ( (phase>obsphase.max())).any():
					decayFactor=10**(-0.4*self.extrapolateDecline*(phase[phase>obsphase.max()]-obsphase.max()))
					modulatedM0[np.where(phase>obsphase.max())[0]] *= decayFactor[:,np.newaxis]
					modulatedM1[np.where(phase>obsphase.max())[0]] *= decayFactor[:,np.newaxis]
						
				modelsynM0flux=np.sum(modulatedM0, axis=1)*dwave*self.fluxfactor[survey][flt]
				modelsynM1flux=np.sum(modulatedM1, axis=1)*dwave*self.fluxfactor[survey][flt]
				
				modulatedFlux= x0*(modulatedM0 +modulatedM1*x1)
				modelflux = x0* (modelsynM0flux+ x1*modelsynM1flux)
				if varyParams[self.parlist=='x0_{}'.format(sn)][0] :
					photresultsdict['modelflux_jacobian'][selectFilter,(varyParList == 'x0_{}'.format(sn))] = modelsynM0flux+ x1*modelsynM1flux
				if varyParams[self.parlist=='x1_{}'.format(sn)][0] :
					photresultsdict['modelflux_jacobian'][selectFilter,(varyParList == 'x1_{}'.format(sn))] = modelsynM1flux*x0
			else:
				modulatedFlux = pbspl[flt]*storedResults['fluxInterp_{}'.format(sn)](clippedPhase)[:,idx[flt]]
				if ( (phase>obsphase.max())).any():
					decayFactor=10**(-0.4*self.extrapolateDecline*(phase[phase>obsphase.max()]-obsphase.max()))
					modulatedFlux[np.where(phase>obsphase.max())[0]] *= decayFactor[:,np.newaxis]
				modelflux = np.sum(modulatedFlux, axis=1)*dwave*self.fluxfactor[survey][flt]
			
			if tpkDeriv:
				#Need to figure out how to handle derivatives wrt time when dealing with nearest neighbor interpolation; maybe require linear?
				modulatedPhaseDeriv= pbspl[flt]*storedResults['phaseDerivInterp_{}'.format(sn)](clippedPhase)[:,idx[flt]]
				photresultsdict['modelflux_jacobian'][selectFilter,(varyParList == 'tpkoff_{}'.format(sn))] =np.sum(modulatedPhaseDeriv, axis=1)*dwave*self.fluxfactor[survey][flt]
				for p in np.where(phase>obsphase.max())[0]:
					photresultsdict['modelflux_jacobian'][np.where(selectFilter)[0][p],(varyParList=='tpkoff_{}'.format(sn))]=-0.4*np.log(10)*self.extrapolateDecline*modelflux[p]
			if cDeriv:
				#d model / dc is total flux (M0 and M1 components (already modulated with passband)) times the color law and a factor of ln(10)
				photresultsdict['modelflux_jacobian'][selectFilter,(varyParList == 'c_{}'.format(sn))]=np.sum((modulatedFlux)*np.log(10)*colorlaw[np.newaxis,idx[flt]], axis=1)*dwave*self.fluxfactor[survey][flt]
				
			#Multiply M0 and M1 components (already modulated with passband) by c* d colorlaw / d cl_i, with associated normalizations
			for i,varIndex in enumerate(np.where(varyParList=='cl')[0]): 
				photresultsdict['modelflux_jacobian'][selectFilter,varIndex]= (np.sum((modulatedFlux)[:,:]*self.colorLawDeriv[np.newaxis,idx[flt],i], axis=1)*-0.4*np.log(10)*c*dwave*self.fluxfactor[survey][flt])			
			
			if requiredPCDerivs.any() and not 'pcDeriv_phot_%s'%sn in storedResults:
				passbandColorExp=pbspl[flt]*colorexp[idx[flt]]*self.datadict[sn]['mwextcurve'][idx[flt]]
				for pdx,p in enumerate(np.where(selectFilter)[0]):
					derivInterp = self.spline_deriv_interp(
						(clippedPhase[pdx]/(1+z),self.wave[idx[flt]]),
						method=self.interpMethod)[:,requiredPCDerivs]
					summation = np.sum( passbandColorExp.T * derivInterp, axis=0)
					if phase[pdx]>obsphase.max():
						decayFactor= 10**(-0.4*self.extrapolateDecline*(phase[pdx]-obsphase.max()))
						summation*=decayFactor
					if requiredPCDerivs.all():
						summationCache[p,:]=summation
					photresultsdict['modelflux_jacobian'][p,(varyParList=='m0')]=summation[varyParams[self.im0][requiredPCDerivs]]*intmult
					photresultsdict['modelflux_jacobian'][p,(varyParList=='m1')]=summation[varyParams[self.im1][requiredPCDerivs]]*intmult*x1
			
			photresultsdict['modelflux'][selectFilter]=modelflux
		if 'pcDeriv_phot_%s'%sn in storedResults:
			if requiredPCDerivs.any():
				photresultsdict['modelflux_jacobian'][:,(varyParList=='m0')]=summation[:,varyParams[self.im0]]*intmult
				photresultsdict['modelflux_jacobian'][:,(varyParList=='m1')]=summation[:,varyParams[self.im1]]*intmult*x1
		elif requiredPCDerivs.all():
			storedResults['pcDeriv_phot_%s'%sn]=summationCache
		return photresultsdict
			
	def specValsForSN(self,x,sn,storedResults,varyParams):
		z = self.datadict[sn]['zHelio']
		survey = self.datadict[sn]['survey']
		filtwave = self.kcordict[survey]['filtwave']
		obswave = self.datadict[sn]['obswave'] #self.wave*(1+z)
		obsphase = self.datadict[sn]['obsphase'] #self.phase*(1+z)
		specdata = self.datadict[sn]['specdata']
		pbspl = self.datadict[sn]['pbspl']
		dwave = self.datadict[sn]['dwave']
		idx = self.datadict[sn]['idx']
		x0,x1,c,tpkoff = x[self.parlist == 'x0_%s'%sn],x[self.parlist == 'x1_%s'%sn],\
						 x[self.parlist == 'c_%s'%sn],x[self.parlist == 'tpkoff_%s'%sn]

		xDeriv=varyParams[self.parlist=='x0_{}'.format(sn)][0] or varyParams[self.parlist=='x1_{}'.format(sn)][0] 
		tpkDeriv=varyParams[self.parlist=='tpkoff_{}'.format(sn)][0]
		cDeriv=varyParams[self.parlist=='c_{}'.format(sn)][0]
		requiredPCDerivs=varyParams[self.im0]|varyParams[self.im1]
		
		varyParList=self.parlist[varyParams]

		nspecdata = sum([specdata[key]['flux'].size for key in specdata])
		specresultsdict={}
		specresultsdict['modelflux'] = np.zeros(nspecdata)
		specresultsdict['dataflux'] = np.zeros(nspecdata)
		specresultsdict['modelflux_jacobian'] = np.zeros((nspecdata,varyParams.sum()))
		
		if requiredPCDerivs.all() and not 'pcDeriv_spec_%s'%sn in storedResults:
			interpCache=np.zeros((nspecdata,self.im0.size))

		iSpecStart = 0
		for k in specdata.keys():
			SpecLen = specdata[k]['flux'].size
			phase=specdata[k]['tobs']+tpkoff
			specSlice=slice(iSpecStart,iSpecStart+SpecLen)
			specresultsdict['dataflux'][specSlice] = specdata[k]['flux']

			#Define recalibration factor
			coeffs=x[self.parlist=='specrecal_{}_{}'.format(sn,k)]
			pow=coeffs.size-1-np.arange(coeffs.size)
			coeffs/=factorial(pow)
			recalexp = np.exp(np.poly1d(coeffs)((specdata[k]['wavelength']-np.mean(specdata[k]['wavelength']))/self.specrange_wavescale_specrecal))

			if xDeriv:
				M0interp = storedResults['componentsInterp_%s'%sn][0](phase)
				M0int = interp1d(obswave,M0interp[0],kind=self.interpMethod,bounds_error=False,fill_value=0,assume_sorted=True)
				M0interp = M0int(specdata[k]['wavelength'])*recalexp

				M1interp = storedResults['componentsInterp_%s'%sn][1](phase)
				M1int = interp1d(obswave,M1interp[0],kind=self.interpMethod,bounds_error=False,fill_value=0,assume_sorted=True)
				M1interp = M1int(specdata[k]['wavelength'])*recalexp
				
				x0Deriv=(M0interp + x1*M1interp)
				modulatedFlux = x0*x0Deriv
				specresultsdict['modelflux_jacobian'][specSlice,(varyParList == 'x0_{}'.format(sn))] = x0Deriv[:,np.newaxis]
				specresultsdict['modelflux_jacobian'][specSlice,(varyParList == 'x1_{}'.format(sn))] = x0*M1interp[:,np.newaxis]

				specresultsdict['modelflux'][specSlice] = modulatedFlux

			else:
				modinterp = storedResults['fluxInterp_%s'%sn](phase)
				modint = interp1d(obswave,modinterp[0],kind=self.interpMethod,bounds_error=False,fill_value=0,assume_sorted=True)
				modulatedFlux = modint(specdata[k]['wavelength'])*recalexp
			specresultsdict['modelflux'][specSlice] = modulatedFlux
		
			if tpkDeriv:
				phaseDerivInterp = storedResults['phaseDerivInterp_%s'%sn](phase)
				phaseDerivInterp = interp1d(obswave,phaseDerivInterp[0],kind=self.interpMethod,bounds_error=False,fill_value=0,assume_sorted=True)(specdata[k]['wavelength'])
				modPhaseDeriv = phaseDerivInterp*recalexp
				specresultsdict['modelflux_jacobian'][specSlice,varyParList == 'tpkoff_{}'.format(sn)] = modPhaseDeriv[:,np.newaxis]

			#Check spectrum is inside proper phase range, extrapolate decline if necessary
			#	saltfluxinterp*=10**(-0.4* self.extrapolateDecline* (phase-obsphase.max()))

			if self.specrecal:
				varySpecRecal=varyParams[self.parlist == 'specrecal_{}_{}'.format(sn,k)]
				drecaltermdrecal=(((specdata[k]['wavelength']-np.mean(specdata[k]['wavelength']))/self.specrange_wavescale_specrecal)[:,np.newaxis] ** (pow)[np.newaxis,varySpecRecal]) / factorial(pow)[np.newaxis,varySpecRecal]
				specresultsdict['modelflux_jacobian'][specSlice,(varyParList == 'specrecal_{}_{}'.format(sn,k))]  = modulatedFlux[:,np.newaxis] * drecaltermdrecal

			#import pdb; pdb.set_trace()
			# derivatives....
			if cDeriv or varyParams[self.iCL].any() or requiredPCDerivs.any():
				colorlawinterp=storedResults['colorLawInterp'](specdata[k]['wavelength']/(1+z))
				colorexpinterp=10**(c*colorlawinterp)
				
			if cDeriv:
				specresultsdict['modelflux_jacobian'][specSlice,varyParList == 'c_{}'.format(sn)] = (modulatedFlux *np.log(10)*colorlawinterp)[:,np.newaxis]

			# color law
			if varyParams[self.iCL].any():
				specresultsdict['modelflux_jacobian'][specSlice,(varyParList=='cl')] = modulatedFlux[:,np.newaxis]*-0.4*np.log(10)*c*self.colorLawDerivInterp(specdata[k]['wavelength']/(1+z))[:,varyParams[self.iCL]]
				
				# M0, M1
			if (requiredPCDerivs).any():
				intmult = _SCALE_FACTOR/(1+z)*recalexp*colorexpinterp*self.datadict[sn]['mwextcurveint'](specdata[k]['wavelength'])
				if 'pcDeriv_spec_%s'%sn in storedResults:
					derivInterp= storedResults['pcDeriv_spec_%s'%sn][specSlice,:]
				else:
					derivInterp = self.spline_deriv_interp((phase[0]/(1+z),specdata[k]['wavelength']/(1+z)),method=self.interpMethod)*intmult[:,np.newaxis]
					if requiredPCDerivs.all() and not 'pcDeriv_spec_%s'%sn in storedResults:
						interpCache[specSlice,:] = derivInterp
				specresultsdict['modelflux_jacobian'][specSlice,(varyParList=='m0')]  = derivInterp[:,varyParams[self.im0]]*(x0)
				specresultsdict['modelflux_jacobian'][specSlice,(varyParList=='m1')] =  derivInterp[:,varyParams[self.im1]]*(x1*x0)
# 				if ( (phase>obsphase.max())).any():
# 					if phase > obsphase.max():
# 						#if computePCDerivs != 2:
# 						specresultsdict['modelflux_jacobian'][specSlice,self.im0] *= 10**(-0.4*self.extrapolateDecline*(phase-obsphase.max()))
# 						specresultsdict['modelflux_jacobian'][specSlice,self.im1] *= 10**(-0.4*self.extrapolateDecline*(phase-obsphase.max()))
# 
# 						self.__dict__['dmodelflux_dM0_spec_%s'%sn][specSlice,:] *= 10**(-0.4*self.extrapolateDecline*(phase-obsphase.max()))
# 						#if computePCDerivs != 1:
			iSpecStart += SpecLen
		if requiredPCDerivs.all() and not 'pcDeriv_spec_%s'%sn in storedResults: 
			storedResults['pcDeriv_spec_%s'%sn]=interpCache
		return specresultsdict
		
	def specVarianceForSN(self,x,sn,storedResults,varyParams):
		z = self.datadict[sn]['zHelio']
		survey = self.datadict[sn]['survey']
		filtwave = self.kcordict[survey]['filtwave']
		obswave = self.datadict[sn]['obswave'] #self.wave*(1+z)
		obsphase = self.datadict[sn]['obsphase'] #self.phase*(1+z)
		specdata = self.datadict[sn]['specdata']
		pbspl = self.datadict[sn]['pbspl']
		dwave = self.datadict[sn]['dwave']
		idx = self.datadict[sn]['idx']
		x0,x1,c,tpkoff = x[self.parlist == 'x0_%s'%sn],x[self.parlist == 'x1_%s'%sn],\
						 x[self.parlist == 'c_%s'%sn],x[self.parlist == 'tpkoff_%s'%sn]

		x0Deriv= varyParams[self.parlist=='x0_{}'.format(sn)][0] 
		x1Deriv= varyParams[self.parlist=='x1_{}'.format(sn)][0] 
		tpkDeriv=varyParams[self.parlist=='tpkoff_{}'.format(sn)][0]
		cDeriv=varyParams[self.parlist=='c_{}'.format(sn)][0]
		varyParlist=self.parlist[varyParams]
		
		nspecdata = sum([specdata[key]['flux'].size for key in specdata])
		specresultsdict={}
		specresultsdict['fluxvariance'] =  np.zeros(nspecdata)
		specresultsdict['modelvariance'] =  np.zeros(nspecdata)
		specresultsdict['modelvariance_jacobian']=np.zeros([nspecdata,varyParams.sum()])
		
		iSpecStart = 0
		for k in specdata.keys():
			SpecLen = specdata[k]['flux'].size
			phase=specdata[k]['tobs']+tpkoff

			specSlice=slice(iSpecStart,iSpecStart+SpecLen)
			
			#Define recalibration factor
			specrecalIndex=self.parlist=='specrecal_{}_{}'.format(sn,k)
			coeffs=x[specrecalIndex]
			pow=coeffs.size-1-np.arange(coeffs.size)
			coeffs/=factorial(pow)
			recalexp = np.exp(np.poly1d(coeffs)((specdata[k]['wavelength']-np.mean(specdata[k]['wavelength']))/self.specrange_wavescale_specrecal))

			if x0Deriv or x1Deriv or varyParams[self.imodelerr].any() or varyParams[self.imodelcorr].any():
				modelErrInt = [ interp1d( obswave, interr(phase)[0],kind=self.interpMethod,bounds_error=False,fill_value=0,assume_sorted=True)  for interr in storedResults['uncertaintyComponentsInterp_{}'.format(sn)]]
				modelCorrInt= [ interp1d( obswave, intcorr(phase)[0],kind=self.interpMethod,bounds_error=False,fill_value=0,assume_sorted=True) for intcorr in storedResults['uncertaintyCorrelationsInterp_{}'.format(sn)]]
				
				modelerrnox = [  interr( specdata[k]['wavelength']) *recalexp for interr in (modelErrInt)]
				corr=  [intcorr(specdata[k]['wavelength']) for intcorr in modelCorrInt]
				
				uncertaintyNoX0=  modelerrnox[0]**2  + 2*x1* corr[0]*modelerrnox[0]*modelerrnox[1] + x1**2 *modelerrnox[1]**2
				modelUncertainty=uncertaintyNoX0*x0**2
			else:
				modelErrInt =  interp1d( obswave, storedResults['modelUncertaintyInterp_{}'.format(sn)](phase)[0],kind=self.interpMethod,bounds_error=False,fill_value=0,assume_sorted=True)(specdata[k]['wavelength'])
				modelUncertainty=recalexp**2 *  modelErrInt
				
			specresultsdict['fluxvariance'][specSlice] = specdata[k]['fluxerr']**2
			specresultsdict['modelvariance'][specSlice] = modelUncertainty

			if x0Deriv:
				specresultsdict['modelvariance_jacobian'][specSlice,(varyParlist == 'x0_{}'.format(sn))] = uncertaintyNoX0[:,np.newaxis]*2*x0
			if x1Deriv:
				specresultsdict['modelvariance_jacobian'][specSlice,(varyParlist == 'x1_{}'.format(sn))] = x0**2 * 2*(modelerrnox[0]*modelerrnox[1]*corr[0]+ x1* modelerrnox[1]**2)[:,np.newaxis]

			if cDeriv or varyParams[self.iCL].any() or varyParams[self.imodelerr].any() or varyParams[self.imodelcorr].any():
				colorlawinterp=storedResults['colorLawInterp'](specdata[k]['wavelength']/(1+z))
				colorexpinterp=10**(c*colorlawinterp)

			if cDeriv:
				specresultsdict['modelvariance_jacobian'][specSlice,(varyParlist == 'c_{}'.format(sn))] = (modelUncertainty * 2 *np.log(10)*colorlawinterp)[:,np.newaxis]
				
			if varyParams[self.iCL].any():
				specresultsdict['modelvariance_jacobian'][specSlice,(varyParlist=='cl')] =  2* (-0.4 *np.log(10)*c)*modelUncertainty[:,np.newaxis]*self.colorLawDerivInterp(specdata[k]['wavelength']/(1+z))[:,varyParams[self.iCL]]

			if self.specrecal : 
				drecaltermdrecal=(((specdata[k]['wavelength']-np.mean(specdata[k]['wavelength']))/self.specrange_wavescale_specrecal)[:,np.newaxis] ** pow[np.newaxis,varyParams[specrecalIndex]]) / factorial(pow)[np.newaxis,varyParams[specrecalIndex]]
				specresultsdict['modelvariance_jacobian'][specSlice,varyParlist == 'specrecal_{}_{}'.format(sn,k)]  =  modelUncertainty[:,np.newaxis] * drecaltermdrecal * 2

			if varyParams[self.imodelerr].any() or varyParams[self.imodelcorr].any():
				interpresult=  self.errorspline_deriv_interp((phase[0]/(1+z),specdata[k]['wavelength']/(1+z)),method=self.interpMethod) 
				extinctionexp=(recalexp*colorexpinterp* _SCALE_FACTOR/(1+z)*self.datadict[sn]['mwextcurveint'](specdata[k]['wavelength']))
				specresultsdict['modelvariance_jacobian'][specSlice,(varyParlist=='modelerr_0'  )]   = 2* x0**2  * (extinctionexp *( modelerrnox[0] + corr[0]*modelerrnox[1]*x1))[:,np.newaxis] * interpresult[:,varyParams[self.parlist=='modelerr_0']]
				specresultsdict['modelvariance_jacobian'][specSlice,(varyParlist=='modelerr_1'  )]   = 2* x0**2  * (extinctionexp *(modelerrnox[1]*x1**2 + corr[0]*modelerrnox[0]*x1))[:,np.newaxis] * interpresult[:,varyParams[self.parlist=='modelerr_1']]
				specresultsdict['modelvariance_jacobian'][specSlice,(varyParlist=='modelcorr_01')] = 2* x0**2  * (modelerrnox[1]*modelerrnox[0]*x1)[:,np.newaxis]  * interpresult[:,varyParams[self.parlist=='modelcorr_01']]

			iSpecStart += SpecLen
		
		return specresultsdict

	def photVarianceForSN(self,x,sn,storedResults,varyParams):
		"""Currently calculated only at the effective wavelength of the filter, not integrated over."""
		z = self.datadict[sn]['zHelio']
		survey = self.datadict[sn]['survey']
		filtwave = self.kcordict[survey]['filtwave']
		obswave = self.datadict[sn]['obswave'] #self.wave*(1+z)
		obsphase = self.datadict[sn]['obsphase'] #self.phase*(1+z)
		photdata = self.datadict[sn]['photdata']
		pbspl = self.datadict[sn]['pbspl']
		dwave = self.datadict[sn]['dwave']
		idx = self.datadict[sn]['idx']
		x0,x1,c,tpkoff = x[self.parlist == 'x0_%s'%sn],x[self.parlist == 'x1_%s'%sn],\
						 x[self.parlist == 'c_%s'%sn],x[self.parlist == 'tpkoff_%s'%sn]
		
		x0Deriv= varyParams[self.parlist=='x0_{}'.format(sn)][0] 
		x1Deriv= varyParams[self.parlist=='x1_{}'.format(sn)][0] 
		tpkDeriv=varyParams[self.parlist=='tpkoff_{}'.format(sn)][0]
		cDeriv=varyParams[self.parlist=='c_{}'.format(sn)][0]
		varyParlist=self.parlist[varyParams]
		
		varyParList=self.parlist[varyParams]
		
		photresultsdict={}
		photresultsdict['fluxvariance'] =  np.zeros(len(photdata['filt']))
		photresultsdict['modelvariance'] =  np.zeros(len(photdata['filt']))
		photresultsdict['colorvariance'] = []
		photresultsdict['modelvariance_jacobian']=np.zeros([photdata['filt'].size,varyParList.size])
		
		for flt in np.unique(photdata['filt']):
			lameff= self.datadict[sn]['lambdaeff'][flt]
			#Select data from the appropriate filter filter
			selectFilter=(photdata['filt']==flt)
			phase=photdata['tobs']+tpkoff
			phase=phase[selectFilter]
			clippedPhase=np.clip(phase,obsphase.min(),obsphase.max())
			nphase = len(phase)
			
			#Calculate color scatter
			if self.iclscat.size:
				coeffs=x[self.parlist=='clscat']
				pow=coeffs.size-1-np.arange(coeffs.size)
				coeffs/=factorial(pow)
				lameffPrime=lameff/(1+z)/1000
				colorscat=np.exp(np.poly1d(coeffs)(lameffPrime))
				if colorscat == np.inf:
					print('infinite color scatter!')
					import pdb; pdb.set_trace()
				pow=pow[varyParams[self.parlist=='clscat']]
				dcolorscatdx= colorscat*((lameffPrime) ** (pow) )/ factorial(pow)
			else:
				colorscat=0
				dcolorscatdx=np.array([])

			photresultsdict['colorvariance']+= [(selectFilter,colorscat,dcolorscatdx)]

			fluxfactor=(self.fluxfactor[survey][flt]*(pbspl[flt].sum())*dwave)**2
			
			if x0Deriv or x1Deriv or varyParams[self.imodelerr].any() or varyParams[self.imodelcorr].any():
				modelErrInt = [ interp1d( obswave, interr(clippedPhase) ,kind=self.interpMethod,bounds_error=False,fill_value=0,assume_sorted=True)  for interr in storedResults['uncertaintyComponentsInterp_{}'.format(sn)]]
				modelCorrInt= [ interp1d( obswave, intcorr(clippedPhase),kind=self.interpMethod,bounds_error=False,fill_value=0,assume_sorted=True) for intcorr in storedResults['uncertaintyCorrelationsInterp_{}'.format(sn)]]
				
				modelerrnox = [  interr( lameff)  for interr in (modelErrInt)]
				corr=  [intcorr(lameff) for intcorr in modelCorrInt]
				
				uncertaintyNoX0=  fluxfactor*(modelerrnox[0]**2  + 2*x1* corr[0]*modelerrnox[0]*modelerrnox[1] + x1**2 *modelerrnox[1]**2)
				modelUncertainty=uncertaintyNoX0*x0**2
			else:
				modelErrInt =  interp1d( obswave, storedResults['modelUncertaintyInterp_{}'.format(sn)](clippedPhase),kind=self.interpMethod,bounds_error=False,fill_value=0,assume_sorted=True)(lameff)
				modelUncertainty= fluxfactor *  modelErrInt
			
			photresultsdict['fluxvariance'][selectFilter] = photdata['fluxcalerr'][selectFilter]**2
			photresultsdict['modelvariance'][selectFilter]=  modelUncertainty

			if x0Deriv:
				photresultsdict['modelvariance_jacobian'][selectFilter,(varyParlist == 'x0_{}'.format(sn))] = uncertaintyNoX0*2*x0
			if x1Deriv:
				photresultsdict['modelvariance_jacobian'][selectFilter,(varyParlist == 'x1_{}'.format(sn))] = x0**2 *fluxfactor * 2*(modelerrnox[0]*modelerrnox[1]*corr[0]+ x1* modelerrnox[1]**2)

			if cDeriv or varyParams[self.iCL].any() or varyParams[self.imodelerr].any() or varyParams[self.imodelcorr].any():
				colorlawinterp=storedResults['colorLawInterp'](lameff/(1+z))
				colorexpinterp=10**(c*colorlawinterp)

			if cDeriv:
				photresultsdict['modelvariance_jacobian'][selectFilter,(varyParlist == 'c_{}'.format(sn))] = (modelUncertainty * 2 *np.log(10)*colorlawinterp)

			for i,varIndex in enumerate(np.where(varyParList=='cl')[0]): 
				photresultsdict['modelvariance_jacobian'][selectFilter,varIndex] =  2* (-0.4 *np.log(10)*c)*modelUncertainty*self.colorLawDerivInterp(lameff/(1+z))[varyParams[self.iCL]][i]
			
			if varyParams[self.imodelerr].any() or varyParams[self.imodelcorr].any():
				extinctionexp=(colorexpinterp* _SCALE_FACTOR/(1+z)*self.datadict[sn]['mwextcurveint'](lameff))
			
				interpresult =  self.errorspline_deriv_interp((clippedPhase/(1+z),lameff/(1+z)),method=self.interpMethod) 
				photresultsdict['modelvariance_jacobian'][np.outer(selectFilter,(varyParList=='modelerr_0')  )]  =( 2* fluxfactor* x0**2  * extinctionexp) *((( modelerrnox[0] + corr[0]*modelerrnox[1]*x1))[:,np.newaxis] * interpresult[:,varyParams[self.parlist=='modelerr_0']]).flatten()
				photresultsdict['modelvariance_jacobian'][np.outer(selectFilter,(varyParList=='modelerr_1')  )]  =( 2* fluxfactor* x0**2  * extinctionexp )*(((modelerrnox[1]*x1**2 + corr[0]*modelerrnox[0]*x1))[:,np.newaxis] * interpresult[:,varyParams[self.parlist=='modelerr_1']]).flatten()
				photresultsdict['modelvariance_jacobian'][np.outer(selectFilter,(varyParList=='modelcorr_01'))]  =( 2*fluxfactor* x0**2 *x1) * ((modelerrnox[1]*modelerrnox[0])[:,np.newaxis]  * interpresult[:,varyParams[self.parlist=='modelcorr_01']]).flatten()
					
		return photresultsdict
		
		
	
	def loglikeforSN(self,sn,x,storedResults={},varyParams=None,debug=False,SpecErrScale=1.0):
		
		"""
		Calculates the likelihood of given SALT model to photometric and spectroscopic observations of a single SN 

		Parameters
		----------
		args : tuple or None
			Placeholder that contains all the other variables for multiprocessing quirks

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

		photResidsDict,specResidsDict = self.ResidsForSN(x,sn,storedResults,varyParams,fixUncertainty=False,SpecErrScale=SpecErrScale)

		
		loglike= - (photResidsDict['resid']**2).sum() / 2.   -(specResidsDict['resid']**2).sum()/2.+photResidsDict['lognorm']+specResidsDict['lognorm']
		if (not varyParams is None) and np.any(varyParams):
			grad_loglike=  - (photResidsDict['resid'][:,np.newaxis] * photResidsDict['resid_jacobian']).sum(axis=0) - (specResidsDict['resid'][:,np.newaxis] * specResidsDict['resid_jacobian']).sum(axis=0) + photResidsDict['lognorm_grad'] +specResidsDict['lognorm_grad']
			return loglike,grad_loglike
		else:
			return loglike
				
	def specchi2(self):

		return chi2
	
	def SALTModel(self,x,evaluatePhase=None,evaluateWave=None):
		"""Returns flux surfaces of SALT model"""
		try: m0pars = x[self.m0min:self.m0max]
		except: import pdb; pdb.set_trace()
		try:
			m0 = bisplev(self.phase if evaluatePhase is None else evaluatePhase,
						 self.wave if evaluateWave is None else evaluateWave,
						 (self.phaseknotloc,self.waveknotloc,m0pars,self.bsorder,self.bsorder))
		except:
			import pdb; pdb.set_trace()
			
		if self.n_components == 2:
			m1pars = x[self.parlist == 'm1']
			m1 = bisplev(self.phase if evaluatePhase is None else evaluatePhase,
						 self.wave if evaluateWave is None else evaluateWave,
						 (self.phaseknotloc,self.waveknotloc,m1pars,self.bsorder,self.bsorder))
			components = (m0,m1)
		elif self.n_components == 1:
			components = (m0,)
		else:
			raise RuntimeError('A maximum of two principal components is allowed')
			
		return components

	def SALTModelDeriv(self,x,dx,dy,evaluatePhase=None,evaluateWave=None):
		"""Returns derivatives of flux surfaces of SALT model"""
		try: m0pars = x[self.m0min:self.m0max]
		except: import pdb; pdb.set_trace()
		try:
			m0 = bisplev(self.phase if evaluatePhase is None else evaluatePhase,
						 self.wave if evaluateWave is None else evaluateWave,
						 (self.phaseknotloc,self.waveknotloc,m0pars,self.bsorder,self.bsorder),
						 dx=dx,dy=dy)
		except:
			import pdb; pdb.set_trace()
			
		if self.n_components == 2:
			m1pars = x[self.parlist == 'm1']
			m1 = bisplev(self.phase if evaluatePhase is None else evaluatePhase,
						 self.wave if evaluateWave is None else evaluateWave,
						 (self.phaseknotloc,self.waveknotloc,m1pars,self.bsorder,self.bsorder),
						 dx=dx,dy=dy)
			components = (m0,m1)
		elif self.n_components == 1:
			components = (m0,)
		else:
			raise RuntimeError('A maximum of two principal components is allowed')
			
		return components

	def CorrelationModel(self,x,evaluatePhase=None,evaluateWave=None):
		"""Returns correlation between SALT model components as a function of phase and wavelength"""
		components=[]
		for min,max in zip(self.corrmin,self.corrmax):
			try: errpars = x[min:max]
			except: import pdb; pdb.set_trace()

			components+=[  bisplev(self.phase if evaluatePhase is None else evaluatePhase,
							   self.wave if evaluateWave is None else evaluateWave,
							   (self.errphaseknotloc,self.errwaveknotloc,errpars,self.bsorder,self.bsorder))]
		return components

	
	def ErrModel(self,x,evaluatePhase=None,evaluateWave=None):
		"""Returns modeled variance of SALT model components as a function of phase and wavelength"""
		components=[]
		for min,max in zip(self.errmin,self.errmax):
			try: errpars = x[min:max]
			except: import pdb; pdb.set_trace()

			components+=[  bisplev(self.phase if evaluatePhase is None else evaluatePhase,
							   self.wave if evaluateWave is None else evaluateWave,
							   (self.errphaseknotloc,self.errwaveknotloc,errpars,self.bsorder,self.bsorder))]
		return components

	def getParsGN(self,x):

		m0pars = x[self.parlist == 'm0']
		#m0err = np.zeros(len(x[self.parlist == 'm0']))
		m1pars = x[self.parlist == 'm1']
		#m1err = np.zeros(len(x[self.parlist == 'm1']))
		
		# covmat (diagonals only?)
		m0_m1_cov = np.zeros(len(m0pars))

		modelerrpars = x[self.parlist == 'modelerr']
		#modelerrerr = np.zeros(len(x[self.parlist == 'modelerr']))

		clpars = x[self.parlist == 'cl']
		clerr = np.zeros(len(x[self.parlist == 'cl']))
		
		clscatpars = x[self.parlist == 'clscat']
		clscat=np.exp(np.poly1d(clscatpars)(self.wave/1000))

		#clscaterr = x[self.parlist == 'clscaterr']
		

		resultsdict = {}
		n_sn = len(self.datadict.keys())
		for k in self.datadict.keys():
			resultsdict[k] = {'x0':x[self.parlist == 'x0_%s'%k],
							  'x1':x[self.parlist == 'x1_%s'%k],# - np.mean(x[self.ix1]),
							  'c':x[self.parlist == 'c_%s'%k],
							  'tpkoff':x[self.parlist == 'tpkoff_%s'%k],
							  'x0err':x[self.parlist == 'x0_%s'%k],
							  'x1err':x[self.parlist == 'x1_%s'%k],
							  'cerr':x[self.parlist == 'c_%s'%k],
							  'tpkofferr':x[self.parlist == 'tpkoff_%s'%k]}


		m0 = bisplev(self.phase,self.wave,(self.phaseknotloc,self.waveknotloc,m0pars,self.bsorder,self.bsorder))
		#m0errp = bisplev(self.phase,self.wave,(self.phaseknotloc,self.waveknotloc,m0pars+m0err,self.bsorder,self.bsorder))
		#m0errm = bisplev(self.phase,self.wave,(self.phaseknotloc,self.waveknotloc,m0pars-m0err,self.bsorder,self.bsorder))
		#m0err = (m0errp-m0errm)/2.
		if len(m1pars):
			m1 = bisplev(self.phase,self.wave,(self.phaseknotloc,self.waveknotloc,m1pars,self.bsorder,self.bsorder))
			#m1errp = bisplev(self.phase,self.wave,(self.phaseknotloc,self.waveknotloc,m1pars+m1err,self.bsorder,self.bsorder))
			#m1errm = bisplev(self.phase,self.wave,(self.phaseknotloc,self.waveknotloc,m1pars-m1err,self.bsorder,self.bsorder))
			#m1err = (m1errp-m1errm)/2.
		else:
			m1 = np.zeros(np.shape(m0))
			#m1err = np.zeros(np.shape(m0))

		#cov_m0_m1 = bisplev(self.phase,self.wave,(self.phaseknotloc,self.waveknotloc,m0_m1_cov,self.bsorder,self.bsorder))
		modelerr = bisplev(self.phase,self.wave,(self.errphaseknotloc,self.errwaveknotloc,modelerrpars,self.bsorder,self.bsorder))
		modelerr[:] = 0.0
		#clscat = splev(self.wave,(self.errwaveknotloc,clscatpars,3))
		if not len(clpars): clpars = []

		# model errors
		m0err,m1err = self.ErrModel(x)
		cov_m0_m1 = self.CorrelationModel(x)[0]*m0err*m1err
		
		return(x,self.phase,self.wave,m0,m0err,m1,m1err,cov_m0_m1,modelerr,
			   clpars,clerr,clscat,resultsdict)

	def getPars(self,loglikes,x,nburn=500,mkplots=False):

		axcount = 0; parcount = 0
		from matplotlib.backends.backend_pdf import PdfPages
		pdf_pages = PdfPages('%s/MCMC_hist.pdf'%self.outputdir)
		fig = plt.figure()
		
		m0pars = np.array([])
		m0err = np.array([])
		for i in self.im0:
			m0pars = np.append(m0pars,x[i,nburn:].mean())#/_SCALE_FACTOR)
			m0err = np.append(m0err,x[i,nburn:].std())#/_SCALE_FACTOR)
			if mkplots:
				if not parcount % 9:
					subnum = axcount%9+1
					ax = plt.subplot(3,3,subnum)
					axcount += 1
					md,std = np.mean(x[i,nburn:]),np.std(x[i,nburn:])
					histbins = np.linspace(md-3*std,md+3*std,50)
					ax.hist(x[i,nburn:],bins=histbins)
					ax.set_title('M0')
					if axcount % 9 == 8:
						pdf_pages.savefig(fig)
						fig = plt.figure()
				parcount += 1

		m1pars = np.array([])
		m1err = np.array([])
		parcount = 0
		for i in self.im1:
			m1pars = np.append(m1pars,x[i,nburn:].mean())
			m1err = np.append(m1err,x[i,nburn:].std())
			if mkplots:
				if not parcount % 9:
					subnum = axcount%9+1
					ax = plt.subplot(3,3,subnum)
					axcount += 1
					md,std = np.mean(x[i,nburn:]),np.std(x[i,nburn:])
					histbins = np.linspace(md-3*std,md+3*std,50)
					ax.hist(x[i,nburn:],bins=histbins)
					ax.set_title('M1')
					if axcount % 9 == 8:
						pdf_pages.savefig(fig)
						fig = plt.figure()
				parcount += 1

		# covmat (diagonals only?)
		m0_m1_cov = np.zeros(len(m0pars))
		chain_len = len(m0pars)
		m0mean = np.repeat(x[self.im0,nburn:].mean(axis=0),np.shape(x[self.im0,nburn:])[0]).reshape(np.shape(x[self.im0,nburn:]))
		m1mean = np.repeat(x[self.im1,nburn:].mean(axis=0),np.shape(x[self.im1,nburn:])[0]).reshape(np.shape(x[self.im1,nburn:]))
		m0var = x[self.im0,nburn:]-m0mean
		m1var = x[self.im1,nburn:]-m1mean
		for i in range(len(m0pars)):
			for j in range(len(m1pars)):
				if i == j: m0_m1_cov[i] = np.sum(m0var[j,:]*m1var[i,:])
		m0_m1_cov /= chain_len


		modelerrpars = np.array([])
		modelerrerr = np.array([])
		for i in np.where(self.parlist == 'modelerr')[0]:
			modelerrpars = np.append(modelerrpars,x[i,nburn:].mean())
			modelerrerr = np.append(modelerrerr,x[i,nburn:].std())

		clpars = np.array([])
		clerr = np.array([])
		for i in self.iCL:
			clpars = np.append(clpars,x[i,nburn:].mean())
			clerr = np.append(clpars,x[i,nburn:].std())

		clscatpars = np.array([])
		clscaterr = np.array([])
		for i in np.where(self.parlist == 'clscat')[0]:
			clscatpars = np.append(clpars,x[i,nburn:].mean())
			clscaterr = np.append(clpars,x[i,nburn:].std())



		result=np.mean(x[:,nburn:],axis=1)

		resultsdict = {}
		n_sn = len(self.datadict.keys())
		for k in self.datadict.keys():
			resultsdict[k] = {'x0':x[self.parlist == 'x0_%s'%k,nburn:].mean(),
							  'x1':x[self.parlist == 'x1_%s'%k,nburn:].mean(),# - x[self.ix1,nburn:].mean(),
							  'c':x[self.parlist == 'c_%s'%k,nburn:].mean(),
							  'tpkoff':x[self.parlist == 'tpkoff_%s'%k,nburn:].mean(),
							  'x0err':x[self.parlist == 'x0_%s'%k,nburn:].std(),
							  'x1err':x[self.parlist == 'x1_%s'%k,nburn:].std(),
							  'cerr':x[self.parlist == 'c_%s'%k,nburn:].std(),
							  'tpkofferr':x[self.parlist == 'tpkoff_%s'%k,nburn:].std()}


		m0 = bisplev(self.phase,self.wave,(self.phaseknotloc,self.waveknotloc,m0pars,self.bsorder,self.bsorder))
		m0errp = bisplev(self.phase,self.wave,(self.phaseknotloc,self.waveknotloc,m0pars+m0err,self.bsorder,self.bsorder))
		m0errm = bisplev(self.phase,self.wave,(self.phaseknotloc,self.waveknotloc,m0pars-m0err,self.bsorder,self.bsorder))
		m0err = (m0errp-m0errm)/2.
		if len(m1pars):
			m1 = bisplev(self.phase,self.wave,(self.phaseknotloc,self.waveknotloc,m1pars,self.bsorder,self.bsorder))
			m1errp = bisplev(self.phase,self.wave,(self.phaseknotloc,self.waveknotloc,m1pars+m1err,self.bsorder,self.bsorder))
			m1errm = bisplev(self.phase,self.wave,(self.phaseknotloc,self.waveknotloc,m1pars-m1err,self.bsorder,self.bsorder))
			m1err = (m1errp-m1errm)/2.
		else:
			m1 = np.zeros(np.shape(m0))
			m1err = np.zeros(np.shape(m0))

		cov_m0_m1 = bisplev(self.phase,self.wave,(self.phaseknotloc,self.waveknotloc,m0_m1_cov,self.bsorder,self.bsorder))
		modelerr = bisplev(self.phase,self.wave,(self.errphaseknotloc,self.errwaveknotloc,modelerrpars,self.bsorder,self.bsorder))
		#phaseout,waveout,fluxout = np.array([]),np.array([]),np.array([])
		#for i in range(len(self.phase)):
		#	for j in range(len(self.wave)):
		#		phaseout = np.append(phaseout,self.phase[i])
		#		waveout = np.append(waveout,self.wave[j])
		#		fluxout = np.append(fluxout,modelerr[i,j])
		#bspl = bisplrep(phaseout,waveout,fluxout,kx=3,ky=3,tx=self.errphaseknotloc,ty=self.errwaveknotloc,task=-1)
		#modelerr2 = bisplev(self.phase,self.wave,bspl)
		#plt.plot(self.wave,modelerr[self.phase == 0,:].flatten())
		#plt.plot(self.wave,modelerr2[self.phase == 0,:].flatten())
		clscat = splev(self.wave,(self.errwaveknotloc,clscatpars,3))
		if not len(clpars): clpars = []

		for snpar in ['x0','x1','c','tpkoff']:
			subnum = axcount%9+1
			ax = plt.subplot(3,3,subnum)
			axcount += 1
			md = np.mean(x[self.parlist == '%s_%s'%(snpar,k),nburn:])
			std = np.std(x[self.parlist == '%s_%s'%(snpar,k),nburn:])
			histbins = np.linspace(md-3*std,md+3*std,50)
			ax.hist(x[self.parlist == '%s_%s'%(snpar,k),nburn:],bins=histbins)
			ax.set_title('%s_%s'%(snpar,k))
			if axcount % 9 == 8:
				pdf_pages.savefig(fig)
				fig = plt.figure()


		pdf_pages.savefig(fig)			
		pdf_pages.close()
		
		return(result,self.phase,self.wave,m0,m0err,m1,m1err,cov_m0_m1,modelerr,
			   clpars,clerr,clscat,resultsdict)

			
	def updateEffectivePoints(self,x):
		"""
		Updates the "effective number of points" constraining a given bin in 
		phase/wavelength space. Should be called any time tpkoff values are recalculated
		
		Parameters
		----------
			
		x : array
			SALT model parameters
					
		"""
		#Clean out array
		self.neffRaw=np.zeros((self.phaseBinCenters.size,self.waveBinCenters.size))
		phaseIndices,waveIndices=np.unravel_index(np.arange(self.im0.size),(self.phaseRegularizationPoints.size,self.waveRegularizationPoints.size))
# 		start=time.time()
# 		spectime,phottime=0,0
		for sn in (self.datadict.keys()):
			tpkoff=x[self.parlist == 'tpkoff_%s'%sn]
			photdata = self.datadict[sn]['photdata']
			specdata = self.datadict[sn]['specdata']
			survey = self.datadict[sn]['survey']
			filtwave = self.kcordict[survey]['filtwave']
			z = self.datadict[sn]['zHelio']
			pbspl = self.datadict[sn]['pbspl']
			obswave=self.datadict[sn]['obswave']
			idx=self.datadict[sn]['idx']
			#For each spectrum, add one point to each bin for every spectral measurement in that bin
# 			spectime-=time.time()
			for k in specdata.keys():
				restWave=specdata[k]['wavelength']/(1+z)
				restWave=restWave[(restWave>self.waveBins[0][0])&(restWave<self.waveBins[1][-1])]
				phase=(specdata[k]['tobs']+tpkoff)/(1+z)
				phaseAffected= (phase>self.phaseBins[0]) & (phase<self.phaseBins[1])
				if phase <= self.phaseBins[0][0]:
					phaseAffected[0]=True
				elif phase>= self.phaseBins[1][-1]:
					phaseAffected[-1]=True
					
				waveAffected= (restWave.min()<self.waveBins[1])&(restWave.max()>self.waveBins[0])
				basisAffected=(phaseAffected[:,np.newaxis] & waveAffected[np.newaxis,:]).flatten()
				
				result=self.spline_deriv_interp((phase, restWave),method=self.interpMethod )[:,basisAffected].sum(axis=0)
				if phase>=self.phaseBins[1][-1]: result*=10**(-0.4*self.extrapolateDecline*((1+z)*(phase-self.phaseBins[1][-1])))
				self.neffRaw[np.where(basisAffected.reshape(self.neffRaw.shape))]+=result
# 			spectime+=time.time()
# 			phottime-=time.time()
			#For each photometric filter, weight the contribution by  
# 			for flt in np.unique(photdata['filt']):
# 				selectFilter=(photdata['filt']==flt)
# 				phase=(photdata['tobs'][selectFilter]+tpkoff)/(1+z)
# 				
# 				waveAffected= (self.waveBins[1] > (self.kcordict[survey][flt]['minlam']/(1+z))) & (self.waveBins[0] < (self.kcordict[survey][flt]['maxlam']/(1+z)))
# 				phaseAffected= ((phase[:,np.newaxis]>self.phaseBins[0][np.newaxis,:])& (phase[:,np.newaxis]<self.phaseBins[1][np.newaxis,:]))
# 				phaseAffected[:,0]=phaseAffected[:,0] | (phase<=self.phaseBins[0][0])
# 				phaseAffected[:,-1]=phaseAffected[:,-1] | (phase>=self.phaseBins[1][-1])
# 				basisAffected=(phaseAffected[:,:,np.newaxis] & waveAffected[np.newaxis,np.newaxis,:]).reshape((phase.size,self.im0.size))
# 				for pdx,p in enumerate(np.where(selectFilter)[0]):
# 					
# 					derivInterp = self.spline_deriv_interp((phase[pdx],self.wave[idx[flt]]),method=self.interpMethod)[:,basisAffected[pdx]]
# 					
# 					summation = np.sum( pbspl[flt].reshape((pbspl[flt].size,1)) * derivInterp, axis=0)/ np.sum(pbspl[flt])
# 					if phase[pdx]>=self.phaseBins[1][-1]: summation*=10**(-0.4*self.extrapolateDecline*((1+z)*(phase[pdx]-self.phaseBins[1][-1])))
# 					self.neffRaw[np.where(basisAffected[pdx].reshape(self.neffRaw.shape))]+=summation
# 			phottime+=time.time()
# 		print('Time for total neff is ',time.time()-start)
# 		print('Spectime: ',spectime,'Phottime: ',phottime)
		#import pdb; pdb.set_trace()
		#Smear it out a bit along phase axis
		#self.neff=gaussian_filter1d(self.neff,1,0)
		self.neffRaw=interp2d(self.waveBinCenters,self.phaseBinCenters,self.neffRaw)(self.waveRegularizationPoints,self.phaseRegularizationPoints)		
		
		# hack!
		self.plotEffectivePoints([-12.5,0,12.5,40],'neff.png')
		self.plotEffectivePoints(None,'neff-heatmap.png')
		self.neff=np.clip(self.neffRaw,1e-4,None)
		
		self.neff[self.neff>200]=np.inf
		
	def plotEffectivePoints(self,phases=None,output=None):
		import matplotlib.pyplot as plt
		if phases is None:
			plt.imshow(self.neffRaw,cmap='Greys',aspect='auto')
			xticks=np.linspace(0,self.waveRegularizationPoints[0].size,8,False)
			plt.xticks(xticks,['{:.0f}'.format(self.waveRegularizationPoints[int(x)]) for x in xticks])
			plt.xlabel('$\lambda$ / Angstrom')
			yticks=np.linspace(0,self.phaseRegularizationPoints[0].size,8,False)
			plt.yticks(yticks,['{:.0f}'.format(self.phaseRegularizationPoints[int(x)]) for x in yticks])
			plt.ylabel('Phase / days')
		else:
			inds=np.searchsorted(self.phaseRegularizationPoints,phases)
			# hack!
			for i in inds:
				plt.plot(self.waveRegularizationPoints[:],self.neffRaw[i,:],label='{:.1f} days'.format(self.phaseRegularizationPoints[i]))
			plt.ylabel('$N_eff$')
			plt.xlabel('$\lambda (\AA)$')
			plt.xlim(self.waveRegularizationPoints.min(),self.waveRegularizationPoints.max())
			plt.legend()
		
		if output is None:
			plt.show()
		else:
			plt.savefig(output,dpi=288)
		plt.clf()
	
	def regularizationScale(self,components,fluxes):
		if self.regularizationScaleMethod=='fixed':
			return self.guessScale,[np.zeros(self.im0.size) for component in fluxes]
		elif self.regularizationScaleMethod=='bbandmax':
			maxFlux=[interp1d(self.phase,flux[:,self.bbandoverlap],axis=0,kind=self.interpMethod,bounds_error=True,assume_sorted=True)(0) for flux in components]
			maxB=[np.sum(self.bbandpbspl*mf) for mf in maxFlux]
			derivInterp = self.spline_deriv_interp(
				(0,self.wave[self.bbandoverlap]),
				method=self.interpMethod)
			summation=(derivInterp*self.bbandpbspl[:,np.newaxis]).sum(axis=0)
			return [np.abs(mB) for mB in maxB],[summation*np.sign(mB) for mB in maxB]
		elif self.regularizationScaleMethod == 'rms':
			scale= [np.sqrt(np.mean(flux**2)) for flux in fluxes]
			return scale, [np.mean(flux[:,:,np.newaxis]*self.regularizationDerivs[0],axis=(0,1))/s for s,flux in zip(scale,fluxes)]
		elif self.regularizationScaleMethod=='meanabs':
			scale= [np.mean(np.abs(flux)) for flux in fluxes]
			return scale, [np.mean(np.sign(flux[:,:,np.newaxis])*self.regularizationDerivs[0],axis=(0,1)) for s,flux in zip(scale,fluxes)]
		elif self.regularizationScaleMethod=='mad':
			scale= [np.mean(np.abs(np.median(flux))) for flux in zip(fluxes)]
			return scale, [np.zeros(self.im0.size) for component in fluxes]
		elif self.regularizationScaleMethod=='maxneff':
			iqr=[ (np.percentile(self.neffRaw,80)<self.neffRaw)  for flux in fluxes]
			scale= [np.mean(np.abs(flux[select])) for select,flux in zip(iqr,fluxes)]
			return scale, [np.mean(np.sign(flux[select,np.newaxis])*self.regularizationDerivs[0][select,:],axis=(0)) for s,select,flux in zip(scale,iqr,fluxes)]
		else:
			raise ValueError('Regularization scale method invalid: ',self.regularizationScaleMethod)

	def dyadicRegularization(self,x, storedResults,varyParams):
		phase=self.phaseRegularizationPoints
		wave=self.waveRegularizationPoints
		
		fluxes=self.SALTModel(x,evaluatePhase=phase,evaluateWave=wave)
		dfluxdwave=self.SALTModelDeriv(x,0,1,phase,wave)
		dfluxdphase=self.SALTModelDeriv(x,1,0,phase,wave)
		d2fluxdphasedwave=self.SALTModelDeriv(x,1,1,phase,wave)
		scale,scaleDeriv=self.regularizationScale(storedResults['components'],fluxes)
		resids=[]
		jac=[]
		for i in range(len(fluxes)):
			indices=[self.im0,self.im1][i]
			boolIndex=np.zeros(self.npar,dtype=bool)
			boolIndex[indices]=True

			#Normalization (divided by total number of bins so regularization weights don't have to change with different bin sizes)
			normalization=np.sqrt(1/( (self.waveBins[0].size-1) *(self.phaseBins[0].size-1)))
			#0 if model is locally separable in phase and wavelength i.e. flux=g(phase)* h(wavelength) for arbitrary functions g and h
			numerator=(dfluxdphase[i] *dfluxdwave[i] -d2fluxdphasedwave[i] *fluxes[i] )
			dnumerator=( self.regularizationDerivs[1][:,:,varyParams[indices]]*dfluxdwave[i][:,:,np.newaxis] + self.regularizationDerivs[2][:,:,varyParams[indices]]* dfluxdphase[i][:,:,np.newaxis] - self.regularizationDerivs[3][:,:,varyParams[indices]]* fluxes[i][:,:,np.newaxis] - self.regularizationDerivs[0][:,:,varyParams[indices]]* d2fluxdphasedwave[i][:,:,np.newaxis] )			
			resids += [normalization* (numerator / (scale[i]**2 * self.neff)).flatten()]
			jacobian=np.zeros((resids[-1].size,varyParams.sum()))
			if boolIndex[varyParams].any():
				jacobian[:,boolIndex[varyParams]]=((dnumerator*(scale[i]**2 )- scaleDeriv[i][np.newaxis,np.newaxis,varyParams[indices]]*2*scale[i]*numerator[:,:,np.newaxis])/self.neff[:,:,np.newaxis]*normalization / scale[i]**4  ).reshape(-1, varyParams[indices].sum())
			jac += [jacobian]
		return resids,jac
	
	def phaseGradientRegularization(self, x, storedResults,varyParams):
		phase=self.phaseRegularizationPoints
		wave=self.waveRegularizationPoints
		fluxes=self.SALTModel(x,evaluatePhase=phase,evaluateWave=wave)
		dfluxdphase=self.SALTModelDeriv(x,1,0,phase,wave)
		scale,scaleDeriv=self.regularizationScale(storedResults['components'],fluxes)
		resids=[]
		jac=[]
		for i in range(len(fluxes)):
			indices=[self.im0,self.im1][i]
			boolIndex=np.zeros(self.npar,dtype=bool)
			boolIndex[indices]=True
			#Normalize gradient by flux scale
			normedGrad=dfluxdphase[i]/scale[i]
			#Derivative of normalized gradient with respect to model parameters
			normedGradDerivs=(self.regularizationDerivs[1][:,:,varyParams[indices]] * scale[i] - scaleDeriv[i][np.newaxis,np.newaxis,varyParams[indices]]*dfluxdphase[i][:,:,np.newaxis])/ scale[i]**2
			#Normalization (divided by total number of bins so regularization weights don't have to change with different bin sizes)
			normalization=np.sqrt(1/((self.waveBins[0].size-1) *(self.phaseBins[0].size-1)))
			#Minimize model derivative w.r.t wavelength in unconstrained regions
			resids+= [normalization* ( normedGrad /	self.neff).flatten()]
			jacobian=np.zeros((resids[-1].size,varyParams.sum()))
			if boolIndex[varyParams].any():
				jacobian[:,boolIndex[varyParams]]=normalization*((normedGradDerivs) / self.neff[:,:,np.newaxis]).reshape(-1, varyParams[indices].sum())
			jac+= [jacobian]
				
		return resids,jac
	
	def waveGradientRegularization(self, x, storedResults,varyParams):
		phase=self.phaseRegularizationPoints
		wave=self.waveRegularizationPoints
		fluxes=self.SALTModel(x,evaluatePhase=phase,evaluateWave=wave)
		dfluxdwave=self.SALTModelDeriv(x,0,1,phase,wave)
		scale,scaleDeriv=self.regularizationScale(storedResults['components'],fluxes)
		waveGradResids=[]
		jac=[]
		for i in range(len(fluxes)):
			indices=[self.im0,self.im1][i]
			boolIndex=np.zeros(self.npar,dtype=bool)
			boolIndex[indices]=True

			#Normalize gradient by flux scale
			normedGrad=dfluxdwave[i]/scale[i]
			#Derivative of normalized gradient with respect to model parameters
			normedGradDerivs=(self.regularizationDerivs[2][:,:,varyParams[indices]] * scale[i] - scaleDeriv[i][np.newaxis,np.newaxis,varyParams[indices]]*dfluxdwave[i][:,:,np.newaxis])/ scale[i]**2
			#Normalization (divided by total number of bins so regularization weights don't have to change with different bin sizes)
			normalization=np.sqrt(1/((self.waveBins[0].size-1) *(self.phaseBins[0].size-1)))
			#Minimize model derivative w.r.t wavelength in unconstrained regions
			waveGradResids+= [normalization* ( normedGrad /	self.neff).flatten()]
			jacobian=np.zeros((waveGradResids[-1].size,varyParams.sum()))
			if boolIndex[varyParams].any():
				jacobian[:,boolIndex[varyParams]]=normalization*((normedGradDerivs) / self.neff[:,:,np.newaxis]).reshape(-1, varyParams[indices].sum())
			jac+= [jacobian]

		return waveGradResids,jac
		
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
