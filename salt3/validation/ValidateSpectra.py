import sys,os,sncosmo
import numpy as np
from salt3.util import readutils
from matplotlib import pyplot as plt
from scipy.special import factorial
from matplotlib.backends.backend_pdf import PdfPages
from sncosmo.salt2utils import SALT2ColorLaw
from scipy.interpolate import interp1d
import extinction
from time import time

def flux(salt3dir,obsphase,obswave,z,x0,x1,c,mwebv):
	m0phase,m0wave,m0flux = np.loadtxt('%s/salt3_template_0.dat'%salt3dir,unpack=True)
	m1phase,m1wave,m1flux = np.loadtxt('%s/salt3_template_1.dat'%salt3dir,unpack=True)
	m0flux = m0flux.reshape([len(np.unique(m0phase)),len(np.unique(m0wave))])
	m1flux = m1flux.reshape([len(np.unique(m1phase)),len(np.unique(m1wave))])
	
	try:
		with open('%s/salt3_color_correction.dat'%(salt3dir)) as fin:
			lines = fin.readlines()
		if len(lines):
			for i in range(len(lines)):
				lines[i] = lines[i].replace('\n','')
			colorlaw_salt3_coeffs = np.array(lines[1:5]).astype('float')
			salt3_colormin = float(lines[6].split()[1])
			salt3_colormax = float(lines[7].split()[1])

			salt3colorlaw = SALT2ColorLaw([2800,7000],colorlaw_salt3_coeffs)
	except:
		pass
	
	modelflux = x0*(m0flux + x1*m1flux)*10**(-0.4*c*salt3colorlaw(np.unique(m0wave)))*1e-12/(1+z)

	m0interp = interp1d(np.unique(m0phase)*(1+z),m0flux*10**(-0.4*c*salt3colorlaw(np.unique(m0wave)))*1e-12/(1+z),axis=0,
						kind='nearest',bounds_error=False,fill_value="extrapolate")
	m0phaseinterp = m0interp(obsphase)
	m0interp = np.interp(obswave,np.unique(m0wave)*(1+z),m0phaseinterp)

	m1interp = interp1d(np.unique(m0phase)*(1+z),m1flux*10**(-0.4*c*salt3colorlaw(np.unique(m0wave)))*1e-12/(1+z),axis=0,
						kind='nearest',bounds_error=False,fill_value="extrapolate")
	m1phaseinterp = m1interp(obsphase)
	m1interp = np.interp(obswave,np.unique(m0wave)*(1+z),m1phaseinterp)

	
	intphase = interp1d(np.unique(m0phase)*(1+z),modelflux,axis=0,kind='nearest',bounds_error=False,fill_value="extrapolate")
	modelflux_phase = intphase(obsphase)
	intwave = interp1d(np.unique(m0wave)*(1+z),modelflux_phase,kind='nearest',bounds_error=False,fill_value="extrapolate")
	modelflux_wave = intwave(obswave)
	modelflux_wave = x0*(m0interp + x1*m1interp)
	mwextcurve = 10**(-0.4*extinction.fitzpatrick99(obswave,mwebv*3.1))
	modelflux_wave *= mwextcurve

	return modelflux_wave
	
def compareSpectra(speclist,salt3dir,outdir=None,specfile=None,parfile='salt3_parameters.dat',
				   m0file='salt3_template_0.dat',
				   m1file='salt3_template_1.dat',
				   clfile='salt3_color_correction.dat',
				   cdfile='salt3_color_dispersion.dat',
				   errscalefile='salt3_lc_dispersion_scaling.dat',
				   lcrv00file='salt3_lc_relative_variance_0.dat',
				   lcrv11file='salt3_lc_relative_variance_1.dat',
				   lcrv01file='salt3_lc_relative_covariance_01.dat',
				   ax=None,maxspec=None,base=None,verbose=False):

	plt.close('all')
	trd = time()
	if base:
		datadict=readutils.rdAllData(speclist,False,None,lambda x: None,speclist,KeepOnlySpec=True,peakmjdlist=base.options.tmaxlist)
	else:
		datadict=readutils.rdAllData(speclist,False,None,lambda x: None,speclist,KeepOnlySpec=True,peakmjdlist=None)
	print('reading data took %.1f seconds'%(time()-trd))
	tc = time()
	if base: datadict = base.mkcuts(datadict,KeepOnlySpec=True)
	print('making cuts took %.1f seconds'%(time()-tc))

	salt3 = sncosmo.SALT2Source(modeldir=salt3dir,m0file=m0file,
								m1file=m1file,
								clfile=clfile,cdfile=cdfile,
								errscalefile=errscalefile,
								lcrv00file=lcrv00file,
								lcrv11file=lcrv11file,
								lcrv01file=lcrv01file)
	model=sncosmo.Model(source=salt3)
	parlist,pars=np.loadtxt(os.path.join(salt3dir,parfile),
							skiprows=1,unpack=True,dtype=[('a','U40'),('b',float)])
	if outdir is None: outdir=salt3dir

	#pars[parlist=='specrecal_{}_{}'.format(7194,1)]
	pdf_pages = PdfPages(specfile)
	fig = plt.figure()
	axcount = 0
	for sn in datadict.keys():
		specdata=datadict[sn]['specdata']
		snPars={'z':datadict[sn]['zHelio']}
		try:
			for par in ['x0','x1','c','t0']:
				if par=='t0':
					snPars['t0']=pars[parlist=='tpkoff_{}'.format(sn)][0]
				else:
					snPars[par]=pars[parlist== '{}_{}'.format(par,sn)][0]
		except:
			if verbose: print('SN {} is not in parameters, skipping'.format(sn))
			continue
		model.update(snPars)
		#import pdb; pdb.set_trace()
		for k in specdata.keys():
			if 'hi': #try:
				coeffs=pars[parlist=='specrecal_{}_{}'.format(sn,k)]
				pow=coeffs.size-1-np.arange(coeffs.size)
				coeffs/=factorial(pow)
				wave=specdata[k]['wavelength']
				recalexp=np.exp(np.poly1d(coeffs)((wave-np.mean(wave))/2500))
				
				unncalledModel = flux(salt3dir,specdata[k]['tobs']+snPars['t0'],specdata[k]['wavelength'],
									  snPars['z'],snPars['x0'],snPars['x1'],snPars['c'],mwebv=datadict[sn]['MWEBV'])
				modelflux = unncalledModel*recalexp

				if not axcount % 3 and axcount != 0:
					fig = plt.figure()
				ax = plt.subplot(3,1,axcount % 3 + 1)
			
				if len(coeffs): ax.plot(wave,modelflux,'r-',label='recalibrated model spectrum')
				ax.plot(wave,specdata[k]['flux'],'b-',label='%s spectral data, phase = %.1f'%(sn,specdata[k]['tobs']-snPars['t0']))
				ax.plot(wave,unncalledModel,'g-',label='SALT3 Model spectrum\n(no calibration)')
				ax.set_xlim(wave.min(),wave.max())

				ax.set_ylim(0,specdata[k]['flux'].max()*1.25)
				ax.set_xlabel('Wavelength $\AA$')
				ax.set_ylabel('Flux')
				ax.legend(loc='upper right',prop={'size':8})

				axcount += 1

				if not axcount % 3:
					pdf_pages.savefig()
				if maxspec and axcount >= maxspec:
					break
			else:
				print(e)
				continue

	pdf_pages.savefig()
	pdf_pages.close()
			
if __name__ == "__main__":
	usagestring = """ Compares a SALT3 model to spectra

	usage: python ValidateSpectra.py <speclist> <salt3dir>

	Dependencies: sncosmo?
	"""
	compareSpectra(*sys.argv[1:])
