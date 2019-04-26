from setuptools.command.test import test as TestCommand
from distutils.core import setup, Extension
import numpy.distutils.misc_util
import sys


if sys.version_info < (3,0):
	sys.exit('Sorry, Python 2 is not supported')

class SALT3Test(TestCommand):

	def run_tests(self):
		import salt3
		errno = salt3.test()
		sys.exit(errno)

AUTHOR = 'David Jones, Rick Kessler'
AUTHOR_EMAIL = 'david.jones@ucsc.edu'
VERSION = '0.1dev'
LICENSE = 'BSD'
URL = 'salt3.readthedocs.org'

#c_ext = Extension("salt3.simulation._angSep", ["salt3/simulation/_angSep.c", "salt3/simulation/angSep.c"])
#setup(
#    ext_modules=[c_ext],
#    include_dirs=numpy.distutils.misc_util.get_numpy_include_dirs(),
#)

setup(
	name='SALT3',
	version=VERSION,
	packages=['salt3','salt3.tests','salt3.simulation',
			  'salt3.training','salt3.util','salt3.initfiles','salt3.validation'],
	cmdclass={'test': SALT3Test},
	scripts=['salt3/training/TrainSALT.py','salt3/validation/ValidateModel.py','salt3/validation/ValidateLightcurves.py','salt3/validation/ValidateSpectra.py'],
	package_data={'': ['initfiles/Kaepora_dm15_0.94.txt', 'initfiles/salt2_color_dispersion.dat', 'initfiles/Kaepora_dm15_1.1.txt', 'initfiles/salt2_color_correction.dat', 'initfiles/vegased_2004_stis.txt', 'initfiles/Hsiao07.dat', 'initfiles/Bessell90_B.dat', 'initfiles/flatnu.dat', 'initfiles/salt2_spec_variance_0.dat', 'initfiles/salt2_spec_variance_1.dat', 'initfiles/salt2_lc_relative_covariance_01.dat', 'initfiles/salt2_template_1.dat', 'initfiles/salt2_spec_covariance_01.dat', 'initfiles/salt2_template_0.dat', 'initfiles/salt2_lc_relative_variance_1.dat', 'initfiles/salt2_lc_relative_variance_0.dat', 'initfiles/salt2_lc_dispersion_scaling.dat']},
	include_package_data=True,
	author=AUTHOR,
	author_email=AUTHOR_EMAIL,
	license=LICENSE,
	long_description=open('README.md').read(),
    include_dirs=numpy.distutils.misc_util.get_numpy_include_dirs(),
	install_requires=['numpy>=1.5.0',
					  'scipy>=0.9.0',
					  'extinction>=0.2.2',
					  'astropy>=0.4.0',
					  'pytest-astropy',
					  'sncosmo'],
	)
