#!/usr/bin/env python
import numpy as np
from scipy.interpolate import bisplrep,bisplev
from scipy.interpolate import interp1d
from sncosmo.constants import HC_ERG_AA

_SCALE_FACTOR = 1e-12

def init_hsiao(hsiaofile='initfiles/Hsiao07.dat',
			   Bfilt='initfiles/Bessell90_B.dat',
			   flatnu='initfiles/flatnu.dat',
			   phaserange=[-20,50],waverange=[2000,9200],phaseinterpres=1.0,
			   waveinterpres=2.0,phasesplineres=3.2,wavesplineres=72,
			   days_interp=5,debug=False,normalize=True):

	phase,wave,flux = np.loadtxt(hsiaofile,unpack=True)
	
	refWave,refFlux=np.loadtxt(flatnu,unpack=True)
	# was *6
	iGood = np.where((phase >= phaserange[0]-phasesplineres*0) & (phase <= phaserange[1]+phasesplineres*0) &
					 (wave >= waverange[0]-wavesplineres*0) & (wave <= waverange[1]+wavesplineres*0))[0]
	phase,wave,flux = phase[iGood],wave[iGood],flux[iGood]
	
	if normalize:
		m0flux = flux*10**(-0.4*(-19.49+(synphotB(refWave,refFlux,0,0,Bfilt)-synphotB(wave[phase==0],flux[phase==0],0,0,Bfilt))))#*_SCALE_FACTOR
	else:
		m0flux = flux[:]

	#m1phase = phase*1.1
	# was *3 (phase), *5 (wave)
	splinephase = np.linspace(phaserange[0],phaserange[1],int((phaserange[1]-phaserange[0])/phasesplineres)+1,True)
	splinewave = np.linspace(waverange[0],waverange[1],int((waverange[1]-waverange[0])/wavesplineres)+1,True)
	#splinephase = [-8.2, -3.85, -0.05, 3.65, 7.9, 12.55, 16.9, 20.9, 25.25, 30.95, 38.55, 50, 50.05, 50.05]
	#splinewave = [2198,2262.08,2325.44,2387.36,2449.28,2510.48,2570.24,2630,2689.04,2748.08,2805.68,2863.28,2920.88,2977.76,3033.92,3089.36,3145.52,3200.24,3255.68,3310.4,3364.4,3418.4,3472.4,3526.4,3580.4,3633.68,3686.96,3740.24,3793.52,3846.08,3899.36,3952.64,4005.2,4058.48,4111.04,4164.32,4217.6,4270.16,4323.44,4376.72,4430.72,4484,4538,4592,4646,4700.72,4755.44,4810.16,4865.6,4921.76,4977.2,5034.08,5090.24,5147.84,5205.44,5263.76,5322.08,5381.84,5441.6,5502.08,5562.56,5624.48,5687.12,5750.48,5814.56,5880.08,5945.6,6012.56,6080.96,6150.08,6220.64,6291.92,6365.36,6439.52,6515.84,6593.6,6672.8,6754.16,6836.96,6922.64,7010.48,7101.2,7194.08,7289.84,7389.2,7492.16,7598.72,7709.6,7825.52,7945.76,8072.48,8205.68,8345.36,8494.4,8652.08,8821.28,9003.44,9200,9200.72,9200.72]


	bspl = bisplrep(phase,wave,m0flux,kx=3,ky=3, tx=splinephase,ty=splinewave,task=-1)

	intphase = np.linspace(phaserange[0],phaserange[1]+phaseinterpres,
						   int((phaserange[1]-phaserange[0])/phaseinterpres)+1,False)
	intwave = np.linspace(waverange[0],waverange[1]+waveinterpres,
						  int((waverange[1]-waverange[0])/waveinterpres)+1,False)


	m0 = bisplev(intphase,intwave,bspl)
	
	m1fluxguess = flux*10**(-0.4*(-8.93+(synphotB(refWave,refFlux,0,0,Bfilt)-\
										 synphotB(wave[phase==0],flux[phase==0],0,0,Bfilt))))
	m1fluxguess *= 1e2
	#m1fluxguess -= 2.0933145e-5
	#m1fluxadj = synphotBflux(wave[phase==0],m1fluxguess[phase==0],0,0,Bfilt)
	#import pdb; pdb.set_trace()
	#m1fluxguess -= m1fluxadj
	bsplm1 = bisplrep(phase,wave,
					  m1fluxguess,kx=3,ky=3,
					  tx=splinephase,ty=splinewave,task=-1)
	m1 = bisplev(intphase,intwave,bsplm1)
	if debug:
		import pylab as plt
		plt.ion()
		plt.plot(wave[phase == 0],flux[phase == 0],label='hsiao, phase = 0 days')
		m0test = bisplev(np.array([0]),intwave,(bspl[0],bspl[1],bspl[2],3,3))
		plt.xlim([2000,9200])
		plt.legend()
		plt.plot(intwave,m0test,label='interp')
		bspltmp = bspl[2].reshape([len(splinephase)-4,len(splinewave)-4])
	#import pdb; pdb.set_trace()
	return intphase,intwave,m0,m1,bspl[0],bspl[1],bspl[2],bsplm1[2]

def init_kaepora(x10file='initfiles/Kaepora_dm15_1.1.txt',
				 x11file='initfiles/Kaepora_dm15_0.94.txt',
				 Bfilt='initfiles/Bessell90_B.dat',
				 flatnu='initfiles/flatnu.dat',
				 phaserange=[-20,50],waverange=[2000,9200],phaseinterpres=1.0,
				 waveinterpres=2.0,phasesplineres=3.2,wavesplineres=72,
				 days_interp=5,debug=False,normalize=True):

	phase,wave,flux = np.loadtxt(x10file,unpack=True)
	x11phase,x11wave,x11flux = np.loadtxt(x11file,unpack=True)
	refWave,refFlux=np.loadtxt(flatnu,unpack=True)

	if normalize:
		m0flux = flux*10**(-0.4*(-19.49+(synphotB(refWave,refFlux,0,0,Bfilt)-synphotB(wave[phase==0],flux[phase==0],0,0,Bfilt))))#*_SCALE_FACTOR
	else:
		m0flux = flux[:]
		
	#m1phase = phase*1.1
	splinephase = np.linspace(phaserange[0],phaserange[1],
							  (phaserange[1]-phaserange[0])/phasesplineres,False)
	splinewave = np.linspace(waverange[0],waverange[1],
							 (waverange[1]-waverange[0])/wavesplineres,False)
	bspl = bisplrep(phase,wave,m0flux,kx=3,ky=3,
					tx=splinephase,ty=splinewave,task=-1)

	intphase = np.linspace(phaserange[0],phaserange[1],
						   (phaserange[1]-phaserange[0])/phaseinterpres,False)
	intwave = np.linspace(waverange[0],waverange[1],
						  (waverange[1]-waverange[0])/waveinterpres,False)


	m0 = bisplev(intphase,intwave,bspl)
	m1fluxguess = (x11flux-flux)*10**(-0.4*(-19.49+(synphotB(refWave,refFlux,0,0,Bfilt)-synphotB(wave[phase==0],flux[phase==0],0,0,Bfilt)))) #10**(-0.4*(-8.93+(synphotB(refWave,refFlux,0,0,Bfilt)-synphotB(wave[phase==0],x11flux[phase==0] - flux[phase==0],0,0,Bfilt))))
	bsplm1 = bisplrep(phase,wave,
					  m1fluxguess,kx=3,ky=3,
					  tx=splinephase,ty=splinewave,task=-1)
	m1 = bisplev(intphase,intwave,bsplm1)
	if debug:
		import pylab as plt
		plt.ion()
		plt.plot(wave[phase == 0],flux[phase == 0],label='hsiao, phase = 0 days')
		m0test = bisplev(np.array([0]),intwave,(bspl[0],bspl[1],bspl[2],3,3))
		plt.xlim([2000,9200])
		plt.legend()
		plt.plot(intwave,m0test,label='interp')
		bspltmp = bspl[2].reshape([len(splinephase)-4,len(splinewave)-4])

	#import pdb; pdb.set_trace()
	return intphase,intwave,m0,m1,bspl[0],bspl[1],bspl[2],bsplm1[2]

def init_errs(hsiaofile='initfiles/Hsiao07.dat',
			  Bfilt='initfiles/Bessell90_B.dat',
			  phaserange=[-20,50],waverange=[2000,9200],phaseinterpres=1.0,
			  waveinterpres=2.0,phasesplineres=6,wavesplineres=1200,
			  days_interp=5,debug=False):

	phase,wave,flux = np.loadtxt(hsiaofile,unpack=True)
	
	m1phase = phase*1.1
	splinephase = np.linspace(phaserange[0],phaserange[1],
							  (phaserange[1]-phaserange[0])/phasesplineres,False)
	splinewave = np.linspace(waverange[0],waverange[1],
							 (waverange[1]-waverange[0])/wavesplineres,False)

	bspl = bisplrep(phase,wave,flux,kx=3,ky=3,
					tx=splinephase,ty=splinewave,task=-1)

	return bspl[0],bspl[1]

def init_errs_fromfile(hsiaofile='initfiles/Hsiao07.dat',
					   Bfilt='initfiles/Bessell90_B.dat',
					   phaserange=[-20,50],waverange=[2000,9200],phaseinterpres=1.0,
					   waveinterpres=2.0,phasesplineres=6,wavesplineres=1200,
					   days_interp=5,debug=False):

	phase,wave,flux = np.loadtxt(hsiaofile,unpack=True)
	
	m1phase = phase*1.1
	splinephase = np.linspace(phaserange[0],phaserange[1],
							  (phaserange[1]-phaserange[0])/phasesplineres,False)
	splinewave = np.linspace(waverange[0],waverange[1],
							 (waverange[1]-waverange[0])/wavesplineres,False)

	intphase = np.linspace(phaserange[0],phaserange[1],
						   (phaserange[1]-phaserange[0])/phaseinterpres,False)
	intwave = np.linspace(waverange[0],waverange[1],
						  (waverange[1]-waverange[0])/waveinterpres,False)

	
	bspl = bisplrep(phase,wave,flux,kx=3,ky=3,
					tx=splinephase,ty=splinewave,task=-1)
	knotvals = bisplev(intphase,intwave,bspl)

	return bspl[0],bspl[1],bspl[2]



def get_hsiao(hsiaofile='initfiles/Hsiao07.dat',
			  Bfilt='initfiles/Bessell90_B.dat',
			  phaserange=[-20,50],waverange=[2000,9200],phaseinterpres=1.0,
			  waveinterpres=2.0,phasesplineres=3.2,wavesplineres=72,
			  days_interp=5.0):

	hphase,hwave,hflux = np.loadtxt(hsiaofile,unpack=True)
	#hflux /= 4*np.pi*(10*3.086e18)**2.
	hflux = hflux.reshape([len(np.unique(hphase)),len(np.unique(hwave))])
	
	phaseinterp = np.linspace(phaserange[0]-days_interp,phaserange[1]+days_interp,
							  (phaserange[1]-phaserange[0]+2*days_interp)/phaseinterpres,False)
	waveinterp = np.linspace(waverange[0],waverange[1],(waverange[1]-waverange[0])/waveinterpres,False)

	int1dphase = interp1d(np.unique(hphase),hflux,axis=0,
						  fill_value='extrapolate')
	hflux_phaseinterp = int1dphase(phaseinterp)
	int1dwave = interp1d(np.unique(hwave),hflux_phaseinterp,axis=1,
						 fill_value='extrapolate')
	hflux_phasewaveinterp = int1dwave(waveinterp)
	
	return hflux_phasewaveinterp

def synphotB(sourcewave,sourceflux,zpoff,redshift=0,
			 Bfilt='initfiles/Bessell90_B.dat'):
	obswave = sourcewave*(1+redshift)

	filtwave,filttrans = np.genfromtxt(Bfilt,unpack=True)

	g = (obswave >= filtwave[0]) & (obswave <= filtwave[-1])  # overlap range

	pbspl = np.interp(obswave[g],filtwave,filttrans)
	pbspl *= obswave[g]

	res = np.trapz(pbspl*sourceflux[g]/HC_ERG_AA,obswave[g])/np.trapz(pbspl,obswave[g])
	return(zpoff-2.5*np.log10(res))

def synphotBflux(sourcewave,sourceflux,zpoff,redshift=0,
			 Bfilt='initfiles/Bessell90_B.dat'):
	obswave = sourcewave*(1+redshift)

	filtwave,filttrans = np.genfromtxt(Bfilt,unpack=True)

	g = (obswave >= filtwave[0]) & (obswave <= filtwave[-1])  # overlap range

	pbspl = np.interp(obswave[g],filtwave,filttrans)
	pbspl *= obswave[g]

	res = np.trapz(pbspl*sourceflux[g]/HC_ERG_AA,obswave[g])/np.trapz(pbspl,obswave[g])
	return res
