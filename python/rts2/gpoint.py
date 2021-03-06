# g(pl)-Point - GPLed Telescope pointing model fit, as described in paper by Marc Buie:
#
# ftp://ftp.lowell.edu/pub/buie/idl/pointing/pointing.pdf 
#
# (C) 2015-2016 Petr Kubanek <petr@kubanek.net>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.

import sys
import numpy as np
import libnova
import string

from math import radians,degrees,cos,sin,tan,sqrt,atan2,acos
from lmfit import minimize, Parameters, minimizer

import re

_gem_params = ['id', 'me', 'ma', 'tf', 'ih', 'ch', 'np', 'daf', 'fo']

# name of AltAz parameters
_altaz_params = ['ia', 'tn', 'te', 'npae', 'npoa', 'ie', 'tf']

def flip_ra(a_ra,dec):
	if abs(dec) > 90:
		return (a_ra + 180) % 360
	return a_ra

def flip_dec(a_dec,dec):
	if dec > 90:
		return 180 - a_dec
	elif dec < -90:
		return -180 - a_dec
	return a_dec

def print_model_input(filename,first):
	hdulist = fits.open(filename)
	h = hdulist[0].header
	w = wcs.WCS(h)
	ra,dec = w.all_pix2world(2000,2000,0)
	tar_telra = float(h['TAR_TELRA'])
	tar_teldec = float(h['TAR_TELDEC'])
	if first:
		print "#observatory",h['SITELONG'],h['SITELAT'],h['ELEVATION']
	print(h['IMGID'],h['JD'],h['LST'],tar_telra,tar_teldec,h['AXRA'],h['AXDEC'],ra,dec)

def normalize_az_err(errs):
	return np.array(map(lambda x: x if x < 180 else x - 360, errs % 360))

def normalize_ha_err(errs):
	return np.array(map(lambda x: x if x < 180 else x - 360, errs % 360))

def _str_to_rad(s):
	if s[-1] == 'd':
		return np.radians(float(s[:-1]))
	elif s[-1] == "'" or s[-1] == 'm':
		return np.radians(float(s[:-1]) / 60.0)
	elif s[-1] == '"' or s[-1] == 's':
		return np.radians(float(s[:-1]) / 3600.0)
	return float(s)

class ExtraParam:
	"""Extra parameter - term for model evaluation"""
	def __init__(self,axis,multi,function,params,consts):
		self.axis = axis
		if multi is None:
			self.multi = None
		else:
			self.multi = _str_to_rad(multi)
		# save initial multiplier
		self.__initial_multi = self.multi
		self.function = function
		self.param = params.split(';')
		self.consts = map(float,consts.split(';'))

	def parnum(self):
		"""Number of parameters this extra function uses."""
		return 1

	def parname(self):
		return '_'.join([self.axis,self.function,'_'.join(self.param),'_'.join(map(str,self.consts)).replace('.','_')])

	def __eq__(self,e):
		return self.axis == e.axis and self.function == e.function and self.param == e.param and self.consts == e.consts

	def __str__(self):
		return '{0}\t{1}\t{2}'.format(self.function,';'.join(map(str,self.param)),';'.join(map(str,self.consts)))

class DuplicatedExtra(Exception):
	"""Raised when adding term already present in model terms"""
	def __init__(self,argument):
		super(DuplicatedExtra, self).__init__('duplicated argument:{0}'.format(argument))
		self.argument = argument

class NonExistentExtra(Exception):
	"""Raised when removing term not present in model terms"""
	def __init__(self,argument):
		super(NonExistentExtra, self).__init__('nonexistent term:{0}'.format(argument))
		self.argument = argument

# Computes, output, concetanetes and plot pointing models.
class GPoint:
	"""Main GPoint class. verbose  verbosity of the output"""
	def __init__(self,verbose=0,latitude=None,longitude=None,altitude=None):
		self.aa_ha = None
		self.verbose = verbose
		self.lines = []
		# telescope latitude - north positive
		self.latitude = self.def_latitude = latitude
		self.longitude = self.def_longitude = longitude
		self.altitude = self.def_altitude = altitude
		self.altaz = False # by default, model GEM
		if latitude is not None:
			self.latitude_r = np.radians(latitude)
		self.best = None
		self.name_map = None
		# addtional terms for model - ExtraParam
		self.extra = []
		self.modelfile = None

	def equ_to_hrz(self,ha,dec):
		""" Transform HA-DEC (in radians) vector to ALT-AZ (in degrees) vector"""
		A = np.sin(self.latitude_r) * np.sin(dec) + np.cos(self.latitude_r) * np.cos(dec) * np.cos(ha)
		alt = np.arcsin(A)

		Z = np.arccos(A)
		Zs = np.sin(Z)
		As = (np.cos(dec) * np.sin(ha)) / Zs;
		Ac = (np.sin(self.latitude_r) * np.cos(dec) * np.cos(ha) - np.cos(self.latitude_r) * np.sin(dec)) / Zs;
		Aa = np.arctan2(As,Ac)

		return np.degrees(alt),(np.degrees(Aa) + 360) % 360

	def hrz_to_equ(self,az,alt):
		""" Transform AZ-ALT (in radians) vector to HA-DEC (in degrees) vector"""

		ha = np.arctan2(np.sin(az), (np.cos(az) + np.sin(self.latitude_r) + np.tan(alt) * np.cos(self.latitude_r)))
		dec = np.sin(self.latitude_r) * np.sin(alt) - np.cos(self.latitude_r) * np.cos(alt) * np.cos(az)
		dec = np.arcsin(dec)

		return np.degrees(ha),np.degrees(dec)

	def get_extra_val(self,e,ha,dec,az,el,num):
		if e.param[num] == 'ha':
			return ha
		elif e.param[num] == 'dec':
			return dec
		elif e.param[num] == 'az':
			return az
		elif e.param[num] == 'el':
			return el
		elif e.param[num] == 'zd':
			return (np.pi / 2) - el
		else:
			sys.exit('unknow parameter {0}'.format(e.param[num]))

	def cal_extra(self,e,axis,ha,dec,az,el):
		if e.function == 'offset':
			oax = self.get_extra_val(e,ha,dec,az,el,0)
			if ((oax == az).all() and axis == 'az') or ((oax == el).all() and axis == 'el'):
				return np.array([1] * len(oax))
			else:
				return np.array([0] * len(oax))
		elif e.function == 'sin':
			return np.sin(e.consts[0] * self.get_extra_val(e,ha,dec,az,el,0))
		elif e.function == 'cos':
			return np.cos(e.consts[0] * self.get_extra_val(e,ha,dec,az,el,0))
		elif e.function == 'abssin':
			return np.abs(np.sin(e.consts[0] * self.get_extra_val(e,ha,dec,az,el,0)))
		elif e.function == 'abscos':
			return np.abs(np.cos(e.consts[0] * self.get_extra_val(e,ha,dec,az,el,0)))
		elif e.function == 'tan':
			return np.tan(e.consts[0] * self.get_extra_val(e,ha,dec,az,el,0))
		elif e.function == 'csc':
			return 1.0 / np.sin(e.consts[0] * self.get_extra_val(e,ha,dec,az,el,0))
		elif e.function == 'sec':
			return 1.0 / np.cos(e.consts[0] * self.get_extra_val(e,ha,dec,az,el,0))
		elif e.function == 'cot':
			return 1.0 / np.tan(e.consts[0] * self.get_extra_val(e,ha,dec,az,el,0))
		elif e.function == 'sincos':
			return np.sin(e.consts[0] * self.get_extra_val(e,ha,dec,az,el,0)) * np.cos(e.consts[1] * self.get_extra_val(e,ha,dec,az,el,1))
		elif e.function == 'sinsin':
			return np.sin(e.consts[0] * self.get_extra_val(e,ha,dec,az,el,0)) * np.sin(e.consts[1] * self.get_extra_val(e,ha,dec,az,el,1))
		elif e.function == 'coscos':
			return np.cos(e.consts[0] * self.get_extra_val(e,ha,dec,az,el,0)) * np.cos(e.consts[1] * self.get_extra_val(e,ha,dec,az,el,1))
		else:
			sys.exit('unknow function {0}'.format(e.function))

	def add_extra(self,axis,function,params,consts):
		return self.add_extra_multi(axis,None,function,params,consts)
	
	def add_extra_multi(self,axis,multi,function,params,consts):
		axis = axis.lower()
		if axis == 'alt':
			axis = 'el'
		if axis == 'ha' or axis == 'dec' or axis == 'az' or axis == 'el':
			ep = ExtraParam(axis,multi,function,params,consts)
			try:
				self.extra.index(ep)
				raise DuplicatedExtra(ep.parname)
			except ValueError:
				pass
			self.extra.append(ep)
			return ep
		else:
			raise Exception('invalid axis name: {0}'.format(axis))

	def remove_extra(self,pn):
		"""Remove extra parameter by parameter name"""
		for p in self.extra:
			if p.parname() == pn:
				self.extra.remove(p)
				return
		raise NonExistentExtra(pn)

	def model_ha(self,params,a_ha,a_dec):
		ret = - params['ih'] \
			- params['ch']/np.cos(a_dec) \
			- params['np']*np.tan(a_dec) \
			- (params['me']*np.sin(a_ha) - params['ma']*np.cos(a_ha)) * np.tan(a_dec) \
			- params['tf']*np.cos(self.latitude_r)*np.sin(a_ha) / np.cos(a_dec) \
			- params['daf']*(np.sin(self.latitude_r) * np.tan(a_dec) + np.cos(self.latitude_r) * np.cos(a_ha))
		for e in self.extra:
			if e.axis == 'ha':
				ret += params[e.parname()] * self.cal_extra(e, 'ha', a_ha, a_dec, self.rad_aa_az, self.rad_aa_alt)
		return ret

	def model_dec(self,params,a_ha,a_dec):
		ret = - params['id'] \
			- params['me']*np.cos(a_ha) \
			- params['ma']*np.sin(a_ha) \
			- params['tf']*(np.cos(self.latitude_r) * np.sin(a_dec) * np.cos(a_ha) - np.sin(self.latitude_r) * np.cos(a_dec)) \
			- params['fo']*np.cos(a_ha)
		for e in self.extra:
			if e.axis == 'dec':
				ret += params[e.parname()] * self.cal_extra(e, 'dec', a_ha, a_dec, self.rad_aa_az, self.rad_aa_alt)
		return ret


	def model_az(self,params,a_az,a_el):
		return self.model_az_hadec(params,a_az,a_el,self.rad_aa_ha,self.rad_aa_dec)

	def model_az_hadec(self,params,a_az,a_el,a_ha,a_dec):
		tan_el = np.tan(a_el)
		ret = - params['ia'] \
			+ params['tn']*np.sin(a_az)*tan_el \
			- params['te']*np.cos(a_az)*tan_el \
			- params['npae']*tan_el \
			+ params['npoa']/np.cos(a_el)
		for e in self.extra:
			if e.axis == 'az':
				ret += params[e.parname()] * self.cal_extra(e, 'az', a_ha, a_dec, a_az, a_el)
		return ret

	def model_el(self,params,a_az,a_el):
		return self.model_el_hadec(params, a_az, a_el, self.rad_aa_ha, self.rad_aa_dec)

	def model_el_hadec(self,params,a_az,a_el,a_ha,a_dec):
		ret = - params['ie'] \
			+ params['tn']*np.cos(a_az) \
			+ params['te']*np.sin(a_az) \
			+ params['tf']*np.cos(a_el)
		for e in self.extra:
			if e.axis == 'el':
				ret += params[e.parname()] * self.cal_extra(e, 'el', a_ha, a_dec, a_az, a_el)
		return ret

	# Fit functions.
	# a_ha - target HA (hour angle)
	# r_ha - calculated (real) HA
	# a_dec - target DEC)
	# r_dec - calculated (real) DEC
	# DEC > 90 or < -90 means telescope flipped (DEC axis continues for modelling purposes)
	def fit_model_ha(self,params,a_ha,r_ha,a_dec,r_dec):
		return a_ha - r_ha + self.model_ha(params,a_ha,a_dec)

	def fit_model_dec(self,params,a_ha,r_ha,a_dec,r_dec):
		return a_dec - r_dec + self.model_dec(params,a_ha,a_dec)

	def fit_model_gem(self,params,a_ra,r_ra,a_dec,r_dec):
		if self.verbose > 1:
			print 'computing', self.latitude, self.latitude_r, params, a_ra, r_ra, a_dec, r_dec
		return libnova.angular_separation(np.degrees(a_ra + self.model_ha(params,a_ra,a_dec)),np.degrees(a_dec + self.model_dec(params,a_ra,a_dec)),np.degrees(r_ra),np.degrees(r_dec))

	def fit_model_az(self,params,a_az,r_az,a_el,r_el):
		return a_az - r_az + self.model_az(params,a_az,a_el)

	def fit_model_el(self,params,a_az,r_az,a_el,r_el):
		return a_el - r_el + self.model_el(params,a_az,a_el)

	def fit_model_altaz(self,params,a_az,r_az,a_el,r_el):
		if self.verbose > 1:
			print 'computing', self.latitude, self.latitude_r, params, a_az, r_az, a_el, r_el
		return libnova.angular_separation(np.degrees(a_az + self.model_az(params,a_az,a_el)),np.degrees(a_el + self.model_el(params,a_az,a_el)),np.degrees(r_az),np.degrees(r_el))

	# open file, produce model
	# expected format:
	#  Observation	  MJD	   LST-MNT RA-MNT   DEC-MNT   AXRA	  AXDEC   RA-TRUE  DEC-TRUE
	## observatory <longitude> <latitude> <altitude>
	#02a57222e0002o 57222.260012 233.8937 275.7921  77.0452  -55497734  -46831997 276.0206  77.0643
	# or for alt-az
	#  Observation	  MJD	   LST-MNT   AZ-MNT   ALT-MNT   AXAZ	  AXALT   AZ-TRUE  ALT-TRUE
	## altaz <longitude> <latitude> <altitude>
	#02a57222e0002o 57222.260012 233.8937 275.7921  77.0452  -55497734  -46831997 276.0206  77.0643
	#skip first line, use what comes next. Make correction on DEC based on axis - if above zeropoint + 90 deg, flip DEC (DEC = 180 - DEC)
	def process_files(self,filenames,flips='both'):
		obsmatch = re.compile('#\s*(\S*)\s+(\S*)\s+(\S*)\s+(\S*)\s*')

		frmt = "astrometry"

		rdata = []

		for filename in filenames:
			f = open(filename)
			# skip first line
			f.readline()
			line = f.readline()
			curr_lines = []
			while not(line == ''):
				if line[0] == '#':
					m = obsmatch.match(line)
					if m:
						if m.group(1) in ['observatory','gem']:
							self.altaz = False
						elif m.group(1) in ['altaz']:
							self.altaz = True
						elif m.group(1) in ['altaz-manual']:
							self.altaz = True
							frmt = "manual"
						else:
							curr_lines.append(line.rstrip())
							line = f.readline()
							continue

						if self.latitude is None:
							self.latitude=m.group(3)
						elif self.def_latitude is None and self.latitude!=m.group(2):
							sys.exit('Cannot (yet) perform calculation on two different latitudes: {0} {1}'.format(self.latitude,m.group(3)))
						# others are not yet used..will be interesting for refraction, if included in model
						if self.longitude is None:
							self.longitude=float(m.group(2))
						if self.altitude is None:
							self.altitude=float(m.group(4))
					else:
						curr_lines.append(line.rstrip())
				else:
					curr_lines.append(line.rstrip())
					self.lines.append(curr_lines)
					curr_lines = []
					s = line.split()
					rdata.append(s[:9])

				line = f.readline()

			f.close()
		
		if self.verbose:
			print "Input data",rdata

		if self.latitude is None:
			sys.exit("You must specify latitude! Either through --latitude option, or in input file (on #observatory line).")

		self.latitude_r = np.radians(float(self.latitude))

		data = []

		if self.altaz:
			if frmt == "manual":
				data = [(float(a_az), float(a_alt), float(a_az) + float(e_az), float(a_alt) + float(e_alt), sn, float(mjd)) for sn,mjd,ra,dec,e_alt,e_az,a_alt,a_az in rdata]
			else:
				data = [(float(a_az), float(a_alt), float(r_az), float(r_alt), sn, float(mjd)) for sn,mjd,lst,a_az,a_alt,ax_az,ax_alt,r_az,r_alt in rdata]
		else:
			# data = [(float(lst) - flip_ra(float(a_ra),float(a_dec)), float(a_dec), float(lst) - float(r_ra), flip_dec(float(r_dec),float(a_dec)), sn, float(mjd)) for sn,mjd,lst,a_ra,a_dec,ax_ra,ax_dec,r_ra,r_dec in rdata]
			data = [(float(lst) - float(a_ra), float(a_dec), float(lst) - flip_ra(float(r_ra),float(a_dec)), flip_dec(float(r_dec),float(a_dec)), sn, float(mjd)) for sn,mjd,lst,a_ra,a_dec,ax_ra,ax_dec,r_ra,r_dec in rdata]
			if flips == 'east':
				data = [d for d in data if abs(d[1]) > 90]
			elif flips == 'west':
				data = [d for d in data if abs(d[1]) < 90]

		a_data = np.array(data)
		if self.verbose:
			print "Parsed data",a_data

		if self.altaz:
			self.aa_az = np.array(a_data[:,0],np.float)
			self.aa_alt = np.array(a_data[:,1],np.float)
			self.ar_az = np.array(a_data[:,2],np.float)
			self.ar_alt = np.array(a_data[:,3],np.float)
		else:	
			self.aa_ha = np.array(a_data[:,0],np.float)
			self.aa_dec = np.array(a_data[:,1],np.float)
			self.ar_ha = np.array(a_data[:,2],np.float)
			self.ar_dec = np.array(a_data[:,3],np.float)
	
		self.mjd = np.array(a_data[:,5],np.float)

		# prepare for X ticks positions
		last_mjd = 0
		last_mjd_hour = 0
		self.mjd_ticks = {}
		self.mjd_hours = {}

		for m in range(0,len(self.mjd)):
			jd = self.mjd[m]
			if last_mjd != round(jd):
				last_mjd = round(jd)
				self.mjd_ticks[m] = last_mjd
			if last_mjd_hour != round(jd * 24):
				last_mjd_hour = round(jd * 24)
				self.mjd_hours[m] = jd


		if self.altaz:
			self.rad_aa_az = np.radians(self.aa_az)
			self.rad_aa_alt = np.radians(self.aa_alt)
			self.rad_ar_az = np.radians(self.ar_az)
			self.rad_ar_alt = np.radians(self.ar_alt)

			# transform to ha/dec
			self.aa_ha,self.aa_dec = self.hrz_to_equ(self.rad_aa_az,self.rad_aa_alt)
			self.ar_ha,self.ar_dec = self.hrz_to_equ(self.rad_ar_az,self.rad_ar_alt)

			self.rad_aa_ha = np.radians(self.aa_ha)
			self.rad_aa_dec = np.radians(self.aa_dec)
			self.rad_ar_ha = np.radians(self.ar_ha)
			self.rad_ar_dec = np.radians(self.ar_dec)
		else:
			self.rad_aa_ha = np.radians(self.aa_ha)
			self.rad_aa_dec = np.radians(self.aa_dec)
			self.rad_ar_ha = np.radians(self.ar_ha)
			self.rad_ar_dec = np.radians(self.ar_dec)

			# transform to alt/az
			self.aa_alt,self.aa_az = self.equ_to_hrz(self.rad_aa_ha,self.rad_aa_dec)
			self.ar_alt,self.ar_az = self.equ_to_hrz(self.rad_ar_ha,self.rad_ar_dec)

			self.rad_aa_az = np.radians(self.aa_az)
			self.rad_aa_alt = np.radians(self.aa_alt)
			self.rad_ar_az = np.radians(self.ar_az)
			self.rad_ar_alt = np.radians(self.ar_alt)

		self.diff_ha = self.aa_ha - self.ar_ha
		self.diff_corr_ha = self.diff_ha * np.cos(self.rad_aa_dec)
		self.diff_dec = self.aa_dec - self.ar_dec
		self.diff_angular_hadec = libnova.angular_separation(self.aa_ha,self.aa_dec,self.ar_ha,self.ar_dec)
		self.diff_angular_altaz = libnova.angular_separation(self.aa_az,self.aa_alt,self.ar_az,self.ar_alt)

		self.diff_alt = self.aa_alt - self.ar_alt
		self.diff_az = normalize_az_err(self.aa_az - self.ar_az)
		self.diff_corr_az = self.diff_az * np.cos(self.rad_aa_alt)

	def fit(self, ftol=1.49012e-08, xtol=1.49012e-08, gtol=0.0, maxfev=1000):
		self.params = Parameters()
		if self.altaz:
			self.params.add('ia', value = 0)
			self.params.add('ie', value = 0)
			self.params.add('tn', value = 0)
			self.params.add('te', value = 0)
			self.params.add('npae', value = 0)
			self.params.add('npoa', value = 0)
			self.params.add('tf', value = 0)

			for ep in self.extra:
				self.params.add(ep.parname(), value = 0)

			self.best = minimize(self.fit_model_altaz, self.params, args=(self.rad_aa_az,self.rad_ar_az,self.rad_aa_alt,self.rad_ar_alt),full_output=True,maxfev=maxfev,ftol=ftol,xtol=xtol,gtol=gtol)

		else:
			self.params.add('ih', value = 0)
			self.params.add('id', value = 0)
			self.params.add('ch', value = 0)
			self.params.add('tf', value = 0)
			self.params.add('ma', value = 0)
			self.params.add('me', value = 0)
			self.params.add('np', value = 0)
			self.params.add('tf', value = 0)
			self.params.add('fo', value = 0)
			self.params.add('daf', value = 0)

			for ep in self.extra:
				self.params.add(ep.parname(), value = 0)

			self.best = minimize(self.fit_model_gem, self.params, args=(self.rad_aa_ha,self.rad_ar_ha,self.rad_aa_dec,self.rad_ar_dec),full_output=True,maxfev=maxfev,ftol=ftol,xtol=xtol,gtol=gtol)


		if self.verbose:
			print 'Fit result', self.best.params

		if self.altaz:
			self.f_model_az = self.fit_model_az(self.best.params,self.rad_aa_az,self.rad_ar_az,self.rad_aa_alt,self.rad_ar_alt)
			self.f_model_alt = self.fit_model_el(self.best.params,self.rad_aa_az,self.rad_ar_az,self.rad_aa_alt,self.rad_ar_alt)

			self.diff_model_az = np.degrees(self.f_model_az)
			self.diff_model_alt = np.degrees(self.f_model_alt)

			self.am_ha,self.am_dec = self.hrz_to_equ(self.rad_ar_az - self.f_model_az,self.rad_ar_alt - self.f_model_alt)

			self.diff_model_ha = normalize_ha_err(self.am_ha - self.ar_ha)
			self.diff_model_dec = self.am_dec - self.ar_dec

			self.diff_model_angular = self.fit_model_altaz(self.best.params,self.rad_aa_az,self.rad_ar_az,self.rad_aa_alt,self.rad_ar_alt)
		else:
			# feed parameters to diff, obtain model differences. Closer to zero = better
			self.f_model_ha = self.fit_model_ha(self.best.params,self.rad_aa_ha,self.rad_ar_ha,self.rad_aa_dec,self.rad_ar_dec)
			self.f_model_dec = self.fit_model_dec(self.best.params,self.rad_aa_ha,self.rad_ar_ha,self.rad_aa_dec,self.rad_ar_dec)

			self.diff_model_ha = np.degrees(self.f_model_ha)
			self.diff_model_dec = np.degrees(self.f_model_dec)

			self.am_alt,self.am_az = self.equ_to_hrz(self.rad_ar_ha - self.f_model_ha,self.rad_ar_dec - self.f_model_dec)

			self.diff_model_alt = self.am_alt - self.ar_alt
			self.diff_model_az = normalize_az_err(self.am_az - self.ar_az)

			self.diff_model_angular = self.fit_model_gem(self.best.params,self.rad_aa_ha,self.rad_ar_ha,self.rad_aa_dec,self.rad_ar_dec)

		self.diff_model_corr_az = self.diff_model_az * np.cos(self.rad_aa_alt)
		self.diff_model_corr_ha = self.diff_model_ha * np.cos(self.rad_aa_ha)

		return self.best.params

	def fit_to_extra(self):
		"""Propagates fit to extra parameters (multi). Must be called before fit is used for model operations"""
		for ep in self.extra:
			ep.multi = self.best.params[ep.parname()].value


	def remove_line(self,ind):
		self.rad_aa_az = np.delete(self.rad_aa_az, ind)
		self.rad_ar_az = np.delete(self.rad_ar_az, ind)
		self.rad_aa_alt = np.delete(self.rad_aa_alt, ind)
		self.rad_ar_alt = np.delete(self.rad_ar_alt, ind)

		self.rad_aa_ha = np.delete(self.rad_aa_ha, ind)
		self.rad_ar_ha = np.delete(self.rad_ar_ha, ind)
		self.rad_aa_dec = np.delete(self.rad_aa_dec, ind)
		self.rad_ar_dec = np.delete(self.rad_ar_dec, ind)

		self.aa_ha = np.delete(self.aa_ha, ind)
		self.ar_ha = np.delete(self.ar_ha, ind)
		self.aa_dec = np.delete(self.aa_dec, ind)
		self.ar_dec = np.delete(self.ar_dec, ind)

		self.aa_az = np.delete(self.aa_az, ind)
		self.ar_az = np.delete(self.ar_az, ind)
		self.aa_alt = np.delete(self.aa_alt, ind)
		self.ar_alt = np.delete(self.ar_alt, ind)

		self.diff_ha = np.delete(self.diff_ha, ind)
		self.diff_corr_ha = np.delete(self.diff_corr_ha, ind)
		self.diff_dec = np.delete(self.diff_dec, ind)
		self.diff_angular_hadec = np.delete(self.diff_angular_hadec, ind)
		self.diff_angular_altaz = np.delete(self.diff_angular_altaz, ind)

		self.diff_alt = np.delete(self.diff_alt, ind)
		self.diff_az = np.delete(self.diff_az, ind)
		self.diff_corr_az = np.delete(self.diff_corr_az, ind)

		self.mjd = np.delete(self.mjd, ind)

		ret = self.lines[ind]
		del self.lines[ind]

		return ret

	def filter(self,axis,error,num):
		# find max error
		ax_d = []
		if axis == 'm-azel' or axis == 'm-altaz':
			ax_d.append(self.diff_model_corr_az)
			ax_d.append(self.diff_model_alt)
		elif axis == 'm-hadec':
			ax_d.append(self.diff_model_corr_ha)
			ax_d.append(self.diff_model_dec)
		else:
			ax_d.append(self.__get_data(axis)[0])

		removed = []

		while num > 0:
			mi = np.argmax(np.abs(ax_d[0]))
			max_v = abs(ax_d[0][mi])
			for a in ax_d[1:]:
				ai = np.argmax(np.abs(a))
				max_a = abs(a[ai])
				if max_a > max_v:
					mi = ai
					max_v = max_a

			if self.verbose:
				print 'axis {0} found maximal value {1} at index {2}'.format(axis, max_v, mi)

			if max_v < error:
				if len(removed) == 0:
					return None
				return removed

			removed.append(self.remove_line(mi))
			self.fit()
			self.print_params()
			self.print_stat()
			num -= 1

		return removed

	def print_params(self):
		if self.verbose == False:
			print 'Covariance: {0}'.format(self.best.covar)
			print 'Status: {0}'.format(self.best.status)
			print 'Message: {0}'.format(self.best.lmdif_message)
			print 'Number of evalutaions: {0}'.format(self.best.nfev)
			print 'Ier: {0}'.format(self.best.ier)

		if self.altaz:
			print 'Zero point in AZ (") {0}'.format(degrees(self.best.params['ia'])*3600.0)
			print 'Zero point in ALT (") {0}'.format(degrees(self.best.params['ie'])*3600.0)
			print 'Tilt of az-axis against N (") {0}'.format(degrees(self.best.params['tn'])*3600.0)
			print 'Tilt of az-axis against E (") {0}'.format(degrees(self.best.params['te'])*3600.0)
			print 'Non-perpendicularity of alt to az axis (") {0}'.format(degrees(self.best.params['npae'])*3600.0)
			print 'Non-perpendicularity of optical axis to alt axis (") {0}'.format(degrees(self.best.params['npoa'])*3600.0)
			print 'Tube flexure (") {0}'.format(degrees(self.best.params['tf'])*3600.0)
		else:
			print 'Zero point in DEC (") {0}'.format(degrees(self.best.params['id'])*3600.0)
			print 'Zero point in RA (") {0}'.format(degrees(self.best.params['ih'])*3600.0)
			i = sqrt(self.best.params['me']**2 + self.best.params['ma']**2)
			print 'Angle between true and instrumental poles (") {0}'.format(degrees(i)*3600.0)
			print 'Angle between line of pole and true meridian (deg) {0}'.format(degrees(atan2(self.best.params['ma'],self.best.params['me']))*3600.0)
			print 'Telescope tube drop in HA and DEC (") {0}'.format(degrees(self.best.params['tf'])*3600.0)
			print 'Angle between optical and telescope tube axes (") {0}'.format(degrees(self.best.params['np'])*3600.0)
			print 'Mechanical orthogonality of RA and DEC axes (") {0}'.format(degrees(self.best.params['ma'])*3600.0)
			print 'Dec axis flexure (") {0}'.format(degrees(self.best.params['daf'])*3600.0)
			print 'Fork flexure (") {0}'.format(degrees(self.best.params['fo'])*3600.0)

			print 'DIFF_MODEL RA',' '.join(map(str,self.diff_model_ha*3600))
			print 'DIFF_MODEL DEC',' '.join(map(str,self.diff_model_dec*3600))
			print self.get_model_type(),' '.join(map(str,self.best.params))

		for e in self.extra:
			print '{0}\t{1}"\t{2}'.format(e.axis.upper(),np.degrees(self.best.params[e.parname()].value)*3600.0,e)

	def get_model_type(self):
		if self.altaz:
			return 'RTS2_ALTAZ'
		else:
			return 'RTS2_GEM'

	def print_stat(self):
		# calculates root mean squeare of vector/array
		def RMS(vector):
			return np.sqrt(np.mean(np.square(vector)))

		def print_vect_stat(v):
			return 'MIN {0} MAX {1} MEAN {2} RMS {3}'.format(np.min(v),np.max(v),np.mean(v),RMS(v))

		print 'OBSERVATIONS {0}'.format(len(self.diff_angular_hadec))
		print 'RMS RA DIFF',print_vect_stat(self.diff_ha*3600)
		print 'RMS RA CORRECTED DIFF',print_vect_stat(self.diff_corr_ha*3600)
		print 'RMS DEC DIFF RMS',print_vect_stat(self.diff_dec*3600)
		print 'RMS AZ DIFF RMS',print_vect_stat(self.diff_az*3600)
		print 'RMS AZ CORRECTED DIFF RMS',print_vect_stat(self.diff_corr_az*3600)
		print 'RMS ALT DIFF RMS',print_vect_stat(self.diff_alt*3600)
		print 'RMS ANGULAR RADEC SEP DIFF',print_vect_stat(self.diff_angular_hadec*3600)
		print 'RMS ANGULAR ALTAZ SEP DIFF',print_vect_stat(self.diff_angular_altaz*3600)
		print 'RMS ANGULAR SEP DIFF',print_vect_stat((self.diff_angular_altaz if self.altaz else self.diff_angular_hadec)*3600)

		if self.best is not None:
			print 'RMS MODEL RA DIFF',print_vect_stat(self.diff_model_ha*3600)
			print 'RMS MODEL RA CORRECTED DIFF',print_vect_stat(self.diff_model_corr_ha*3600)
			print 'RMS MODEL DEC DIFF',print_vect_stat(self.diff_model_dec*3600)
			print 'RMS MODEL AZ DIFF',print_vect_stat(self.diff_model_az*3600)
			print 'RMS MODEL AZ CORRECTED DIFF',print_vect_stat(self.diff_model_corr_az*3600)
			print 'RMS MODEL ALT DIFF',print_vect_stat(self.diff_model_alt*3600)
			print 'RMS MODEL ANGULAR SEP DIFF',print_vect_stat(self.diff_model_angular*3600)

	# set X axis to MJD data
	def set_x_axis(self,plot):
		import pylab
		def mjd_formatter(x, pos):
			try:
				return self.mjd_ticks[int(x)]
			except KeyError,ke:
				try:
					return self.mjd[int(x)]
				except IndexError,ie:
					return x

		plot.xaxis.set_major_formatter(pylab.FuncFormatter(mjd_formatter))
		plot.set_xticks(self.mjd_ticks.keys())
		plot.set_xticks(self.mjd_hours.keys(),minor=True)
		plot.grid(which='major')
		return plot

	# set Y axis to arcsec distance
	def set_y_axis(self,plot):
		import pylab
		def arcmin_formatter(x, pos):
			return "{0}'".format(int(x / 60))
		ymin,ymax = plot.get_ylim()
		numticks = len(plot.get_yticks())
		mtscale = max(60,60 * int(abs(ymax - ymin) / numticks / 60))
		plot.set_yticks(np.arange(ymin - ymin % 60, ymax - ymax % 60, mtscale))
		plot.set_yticks(np.arange(ymin - ymin % 60, ymax - ymax % 60, mtscale / 6.0), minor=True)
		plot.yaxis.set_major_formatter(pylab.FuncFormatter(arcmin_formatter))
		return plot

	def plot_alt_az(self,grid,contour='',pfact=4):
		import pylab
		polar = pylab.subplot2grid(self.plotgrid,grid[:2],colspan=grid[2],rowspan=grid[3],projection='polar')
		polar.plot(np.radians(self.aa_az - 90),90 - self.aa_alt,'r.')
		polar.plot(np.radians(self.ar_az - 90),90 - self.ar_alt,'g.')
		polar.set_rmax(90)
		polar.set_xticklabels(['E','SE','S','SW','W','NW','N','NE'])
		if contour:
			X = np.radians(self.ar_az - 90)
			Y = 90 - self.ar_alt

			if contour == 'model':
				Z = self.diff_model_angular * 3600
				polar.set_title('Model differences')
			elif contour == 'real':
				Z = self.diff_angular_altaz * 3600 if self.altaz else self.diff_angular_hadec * 3600
				polar.set_title('Real differences')

			xi = np.linspace(np.radians(-90),np.radians(271),num = 360 * pfact)
			yi = np.linspace(min(Y),max(Y),num = 90 * pfact)
			zi = pylab.griddata(X, Y, Z, xi, yi, interp='linear')
			ctf = polar.contourf(xi,yi,zi,cmap='hot')
			cbar = pylab.colorbar(ctf, orientation='horizontal', pad=0.05)
			cbar.set_ticks(range(0,int(max(Z)),60))
			cbar.ax.set_xticklabels(map("{0}'".format,range(0,int(max(Z) / 60))))
		else:
			polar.set_title('Alt-Az distribution')
                return polar

	def __get_data(self,name):
		if self.name_map is None:
			# maps name to data,plot style,label
			name_map = {
				'alt-err':[self.diff_alt*3600,'r.','Alt error'],
				'az-err':[self.diff_az*3600,'y.','Az error'],
				'az-corr-err':[self.diff_corr_az*3600, 'y.', 'AZ alt c error'],
				'dec-err':[self.diff_dec*3600,'b.','Dec error'],
				'ha-err':[self.diff_ha*3600,'g.','HA error'],
				'ha-corr-err':[self.diff_corr_ha*3600,'g.','HA dec c error'],
				'mjd':[self.mjd,'m','MJD'],
				'num':[range(len(self.mjd)),'m','Number'],
				'paz':[self.aa_az,'rx','Azimuth'],
				'az':[self.aa_az,'rx','Azimuth'],
				'alt':[self.aa_alt,'yx','Altitude'],
				'dec':[self.aa_dec,'bx','Dec'],
				'ha':[self.aa_ha,'gx','HA'],
				'real-err':[self.diff_angular_altaz*3600 if self.altaz else self.diff_angular_hadec*3600,'c+','Real angular error']
			}
			# append model output only if model was fitted
			if self.best is not None:
				name_map.update({
					'alt-merr':[self.diff_model_alt*3600,'r*','Alt model error'],
					'az-merr':[self.diff_model_az*3600,'y*','Az model error'],
					'az-corr-merr':[self.diff_model_corr_az*3600,'y*','Az c model error'],
					'dec-merr':[self.diff_model_dec*3600,'b*','Dec model error'],
					'ha-merr':[self.diff_model_ha*3600,'g*','HA model error'],
					'ha-corr-merr':[self.diff_model_ha*3600,'g*','HA dec c model error'],
					'model-err':[self.diff_model_angular*3600,'c+','Model angular error']
				})
		return name_map[string.lower(name)]

	def plot_data(self,p,nx,ny,band,draw):
		"""Plots data in plot."""
		if self.verbose:
			print 'plotting {0} {1}'.format(nx,ny)
		xdata = self.__get_data(nx)
		ydata = self.__get_data(ny)
		p.plot(xdata[0],ydata[0],ydata[1])
		p.set_xlabel(xdata[2])
		p.set_ylabel(ydata[2])
		if band is not None:
			import matplotlib.patches as patches
			band = float(band)
			p.add_patch(patches.Rectangle((min(xdata[0]), -band), max(xdata[0]) - min(xdata[0]), 2*band, alpha=0.7, facecolor='red', edgecolor='none'))
		if draw is not None:
			import matplotlib.pyplot as plt
			for d in draw:
				if d[0] == 'c':
					try:
						x,y,r = map(float,d[1:].split(':'))
					except ValueError,ve:
						x = y = 0
						r = float(d[1:])
					p.add_artist(plt.Circle((x,y), r, fill=False))
				elif d[0] == 'x':
					try:
						x,y,r = map(float,d[1:].split(':'))
					except ValueError,ve:
						x = y = 0
						r = float(d[1:])
					p.add_artist(plt.Line2D([x,x],[y+r,y-r]))
					p.add_artist(plt.Line2D([x-r,x+r],[y,y]))
				else:
					raise Exception('unknow draw element {0}'.format(d))
		return p

	def __gen_plot(self,plots,band):
		import pylab
		plot = []
		grid = []
		draw = []

		i = 0
		rows = 1
		cols = 1
		# process plotting string
		for mg in plots.split(','):
			g = [i,0,1,1]
			if len(mg) == 0:
				raise Exception('empty plot specifier')
			plot_s = re.split('([@%])', mg)
			plot.append(plot_s[0])
			j = 0
			while j < len(plot_s):
				if plot_s[j] == '@':
					j += 1
					if len(draw) < len(plot):
						draw.append([])
					draw[len(plot) - 1].append(plot_s[j])
				elif plot_s[j] == '%':
					if g is None:
						raise Exception('grid can be specified only once')
					j += 1
					grids = plot_s[j].split(':')
					if self.verbose:
						print 'grids',grids
					grid.append(map(int, grids) + g[len(grids):])
					g = None
				j += 1
			if g is not None:
				grid.append(g)
			if len(draw) < len(plot):
				draw.append(None)

			lg = grid[-1]
			rows = max(lg[0] + lg[2],rows)
			cols = max(lg[1] + lg[3],cols)
			i += 1

		self.plotgrid = (rows,cols)
		if self.verbose:
			print 'row {0} cols {1}'.format(rows,cols)
		for i in range(0,len(plot)):
			axnam=plot[i].split(':')
			if len(axnam) < 2:
				sys.exit('invalid plot name - {0} does not contain at least one :'.format(plot[i]))
			g = grid[i]
			if self.verbose:
				print 'grid',g
			p = pylab.subplot2grid(self.plotgrid,g[:2],colspan=g[2],rowspan=g[3])
			if axnam[0] == 'paz':
				ax=axnam[1].split('-')
				if ax[0] == 'contour':
					self.plot_alt_az(g,ax[1])
				else:
					self.plot_alt_az(g)
			else:
				for j in axnam[1:]:
					self.plot_data(p,axnam[0],j,band,draw[i])

	def plot(self,plots,band=None,ofile=None):
		import pylab
		self.__gen_plot(plots,band)
		pylab.tight_layout()
		if ofile is None:
			pylab.show()
		else:
			pylab.savefig(ofile)

	def plot_offsets(self,best,subplot,ha_start,ha_end,dec):
		import pylab

		ha_range = np.arange(ha_start,ha_end,0.01)
		dec_r = np.radians(dec)
		if self.altaz:
			el_offsets = []
			az_offsets = []
			if self.verbose:
				print 'ha\taz\tel\taz_off\tel_off'
			for ha in ha_range:
				ha_r = np.radians(ha)
				el,az = libnova.equ_to_hrz(-ha,dec,0,self.latitude)
				el_off = self.model_el_hadec(best,np.radians(az),np.radians(el),ha_r,dec_r)
				az_off = self.model_az_hadec(best,np.radians(az),np.radians(el),ha_r,dec_r)
				if self.verbose:
					print '{0}\t{1}\t{2}\t{3}\t{4}'.format(ha,az,el,az_off * 3600.0,el_off * 3600.0)
				el_offsets.append(np.degrees(el_off))
				az_offsets.append(np.degrees(az_off))
				
			subplot.plot(ha_range,np.array(az_offsets) * 3600.0,'b-',ha_range,np.array(el_offsets) * 3600.0,'g-')
		else:
			ha_offsets = []
			dec_offsets = []
			if self.verbose:
				print 'ha\tha_off\tdec_off'
			for ha in ha_range:
				ha_r = np.radians(ha)
				ha_off = self.model_ha(best,ha_r,dec_r)
				dec_off = self.model_dec(best,ha_r,dec_r)
				if self.verbose:
					print '{0}\t{1}\t{2}'.format(ha,ha_off * 3600.0,dec_off * 3600.0)
				ha_offsets.append(np.degrees(ha_off))
				dec_offsets.append(np.degrees(dec_off))

			subplot.plot(ha_range,np.array(ha_offsets) * 3600.0,'b-',ha_range, np.array(dec_offsets) * 3600.0,'g-')

	def to_string(self,unit='arcseconds'):
		if self.altaz:
			bbpn = _altaz_params
		else:
			bbpn = _gem_params

		bbp = map(lambda x: self.best.params[x].value, bbpn)

		uv = '"'
		mul = 3600.0

		if unit == 'arcminutes':
			uv = "'"
			mul = 60.0
		elif unit == 'degrees':
			uv = u'\u00B0'
			mul = 1.0

		out = self.get_model_type() + ' ' + (uv + ' ').join(map(lambda x:str(np.degrees(x) * mul),bbp)) + uv
		for e in self.extra:
			out += ('\n{0}\t{1}' + uv + '\t{2}').format(e.axis.upper(),np.degrees(self.best.params[e.parname()].value) * mul,e)
		return out

	def __str__(self):
		return self.to_string()

	def save(self,fn):
		"""Save model to file."""
		f = open(fn,'w+')
		f.write(str(self))
		f.close()

	def load(self,fn):
		f = open(fn)
		# basic parameters
		bp = None
		self.altaz = None
		self.best = minimizer.MinimizerResult()
		self.best.params = Parameters()
		while True:
			l = f.readline()
			if l == '':
				break

			line = l.split()

			if l[0] == '#':
				if self.verbose:
					print 'ignoring comment line {0}'.format(l)
				continue

			if line[0] == 'RTS2_MODEL' or line[0] == 'RTS2_GEM':
				if len(line) != 10:
					raise Exception('invalid number of GEM parameters')
				if self.altaz is not None:
					raise Exception('cannot specify model type twice')

				line = line[1:]
				for pn in _gem_params:
					self.best.params[pn] = minimizer.Parameter()
					self.best.params[pn].value = _str_to_rad(line[0])
					line = line[1:]
				self.altaz = False
			elif line[0] == 'RTS2_ALTAZ':
				if len(line) != 8:
					raise Exception('invalid number of Alt-Az parameters')
				if self.altaz is not None:
					raise Exception('cannot specify model type twice')

				line = line[1:]
				for pn in _altaz_params:
					self.best.params[pn] = minimizer.Parameter()
					self.best.params[pn].value = _str_to_rad(line[0])
					line = line[1:]
				self.altaz = True
			# extra params
			elif len(line) == 5:
				ep = self.add_extra_multi(*line)
				self.best.params[ep.parname()] = minimizer.Parameter()
				self.best.params[ep.parname()].value = ep.multi
			else:
				raise Exception('unknow line: {0}'.format(l))

		f.close()
		if self.altaz is None:
			raise Exception('model type not specified')
		self.modelfile = fn

	def add_model(self,m):
		"""Adds to current model another (compatible) model."""
		if self.altaz != m.altaz:
			raise Exception('cannot add two differnet models')

		if self.altaz:
			bbpn = _altaz_params
		else:
			bbpn = _gem_params

		for p in bbpn:
			self.best.params[p].value += m.best.params[p].value

		for e in self.extra:
			for e2 in m.extra:
				if e == e2:
					e.multi += e2.multi
					m.extra.remove(e2)

		# what left is unique to m2
		self.extra += m.extra

		for e in self.extra:
			try:
				self.best.params[e.parname()].value = e.multi
			except KeyError,ve:
				self.best.params[e.parname()] = minimizer.Parameter()
				self.best.params[e.parname()].value = e.multi
