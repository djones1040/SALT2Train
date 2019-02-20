import numpy as np
import pylab as plt
import sncosmo
import argparse
from salt3.util import snana
from astropy.table import Table

def main(outfile,lcfile,salt3dir,
		 m0file='salt3_template_0.dat',
		 m1file='salt3_template_1.dat',
		 clfile='salt2_color_correction.dat',
		 cdfile='salt2_color_dispersion.dat',
		 errscalefile='salt2_lc_dispersion_scaling.dat',
		 lcrv00file='salt2_lc_relative_variance_0.dat',
		 lcrv11file='salt2_lc_relative_variance_1.dat',
		 lcrv01file='salt2_lc_relative_covariance_01.dat',
		 x0 = None, x1 = None, c = None, t0 = None, fitx1=False,fitc=False):
	plt.clf()

	fitparams_salt3 = []
	if not t0: fitparams_salt3 += ['t0']
	if not x0: fitparams_salt3 += ['x0']
	if not x1 and fitx1: fitparams_salt3 += ['x1']
	if not c and fitc: fitparams_salt3 += ['c']
	
	sn = snana.SuperNova(lcfile)
	sn.FLT = sn.FLT.astype('U20')
	for i in range(len(sn.FLT)):
		if sn.FLT[i] in 'griz':
			sn.FLT[i] = 'sdss%s'%sn.FLT[i]
		elif sn.FLT[i].lower() == 'v':
			sn.FLT[i] = 'swope2::v'
		else:
			sn.FLT[i] = 'csp%s'%sn.FLT[i].lower()

	zpsys='AB'
	data = Table([sn.MJD,sn.FLT,sn.FLUXCAL,sn.FLUXCALERR,
				  np.array([27.5]*len(sn.MJD)),np.array([zpsys]*len(sn.MJD))],
				 names=['mjd','band','flux','fluxerr','zp','zpsys'],
				 meta={'t0':sn.MJD[sn.FLUXCAL == np.max(sn.FLUXCAL)]})
	
	flux = sn.FLUXCAL
	salt2model = sncosmo.Model(source='salt2')
	hsiaomodel = sncosmo.Model(source='hsiao')
	salt3 = sncosmo.SALT2Source(modeldir=salt3dir,m0file=m0file,
								m1file=m1file,
								clfile=clfile,cdfile=cdfile,
								errscalefile=errscalefile,
								lcrv00file=lcrv00file,
								lcrv11file=lcrv11file,
								lcrv01file=lcrv01file)
	salt3model =  sncosmo.Model(salt3)
	fitparams_salt2=['t0', 'x0', 'x1', 'c']
	salt2model.set(z=sn.REDSHIFT_HELIO[0:5])
	result_salt2, fitted_salt2_model = sncosmo.fit_lc(data, salt2model, fitparams_salt2)
	fitparams_hsiao = ['t0','amplitude']
	hsiaomodel.set(z=sn.REDSHIFT_HELIO[0:5])
	result_hsiao, fitted_hsiao_model = sncosmo.fit_lc(data, hsiaomodel, fitparams_hsiao)

	salt3model.set(z=sn.REDSHIFT_HELIO[0:5])
	if x0: salt3model.set(x0=x0)
	if t0: salt3model.set(t0=t0)
	if x1: salt3model.set(x1=x1)
	if c: salt3model.set(c=c)
	if len(fitparams_salt3):
		result_salt3, fitted_salt3_model = sncosmo.fit_lc(data, salt3model, fitparams_salt3)
	else:
		fitted_salt3_model = salt3model
	plotmjd = np.linspace(sn.MJD[sn.FLUXCAL == np.max(sn.FLUXCAL)]-20,
						  sn.MJD[sn.FLUXCAL == np.max(sn.FLUXCAL)]+55,100)
	
	fig = plt.figure(figsize=(15, 5))
	ax1 = fig.add_subplot(131)
	ax2 = fig.add_subplot(132)
	ax3 = fig.add_subplot(133)
	
	for flt,i,ax in zip(['sdssg','sdssr','sdssi'],range(3),[ax1,ax2,ax3]):
		hsiaoflux = fitted_hsiao_model.bandflux(flt, plotmjd,zp=27.5,zpsys='AB')
		salt2flux = fitted_salt2_model.bandflux(flt, plotmjd,zp=27.5,zpsys='AB')
		salt3flux = fitted_salt3_model.bandflux(flt, plotmjd,zp=27.5,zpsys='AB')
		ax.plot(plotmjd,hsiaoflux,color='C0',
				label='Hsiao, $\chi^2_{red} = %.1f$'%(
					result_hsiao['chisq']/result_hsiao['ndof']))
		ax.plot(plotmjd,salt2flux,color='C1',
				label='SALT2, $\chi^2_{red} = %.1f$'%(
					result_salt2['chisq']/result_salt2['ndof']))
		if len(fitparams_salt3):
			ax.plot(plotmjd,salt3flux,color='C2',
					label='SALT3, $\chi^2_{red} = %.1f$'%(
						result_salt3['chisq']/result_salt3['ndof']))
		else:
			ax.plot(plotmjd,salt3flux*1e10,color='C2',
					label='SALT3')

		ax.errorbar(sn.MJD[sn.FLT == flt],sn.FLUXCAL[sn.FLT == flt],
					yerr=sn.FLUXCALERR[sn.FLT == flt],
					fmt='o',label=sn.SNID,color='k')
		ax.set_title(flt)
		ax.set_xlim([sn.MJD[sn.FLUXCAL == np.max(sn.FLUXCAL)]-30,
					 sn.MJD[sn.FLUXCAL == np.max(sn.FLUXCAL)]+55])
		ax.set_ylim([-np.max(sn.FLUXCAL)*1/20.,np.max(sn.FLUXCAL)*1.1])
	ax1.legend()
	plt.savefig(outfile)
	plt.show()
	
	
	
if __name__ == "__main__":
	parser = argparse.ArgumentParser(description='Plot lightcurves from SALT3 model against SALT2 model, Hsiao model, and data')
	parser.add_argument('lcfile',type=str,help='File with supernova fit parameters')
	parser.add_argument('salt3dir',type=str,help='File with supernova fit parameters')
	parser.add_argument('outfile',type=str,nargs='?',default=None,help='File with supernova fit parameters')
	parser=parser.parse_args()
	args=vars(parser)
	if parser.outfile is None:
		sn = snana.SuperNova(parser.lcfile)
		args['outfile']='lccomp_%s.png'%sn.SNID
	main(**args)