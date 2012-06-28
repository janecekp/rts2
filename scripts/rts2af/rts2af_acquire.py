#!/usr/bin/python
# (C) 2011, Markus Wildi, markus.wildi@one-arcsec.org
#
#   usage 
#   rts2af_acquire.py --help
#   
#   not yet see man 1 rts2af_acquire
#
#   Basic usage: rts2af_acquire.py
#
#   rts2af_acquire.py is called by rts2-executor on a target, e.g. 5 (focus),
#   or by the test script rts2af_feed_acquire.py.
#
#   It isn't intended to be used interactively. 
#
#   rts2af_acquire.py's purpose is to acquire the images and then terminate
#   in order rts2-executor can immediately continue with the next target.
#
#   It sets the FOC_DEF value for the clear optical path if it is configured.
#   Clear optical path is defined in section [filter properties] is 0 
#   (second element in the array)
#
#   rts2af_acquire.py's stdin, stdout and stderr are read by rts2-executor. Hence
#   logging is done via rts2.scriptcomm.py.
#
#   The configuration file /etc/rts2/rts2af/rts2af-acquire.cfg is hardwired below
#   because EXEC can't (yet) execute scripts with arguments. 
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; either version 2, or (at your option)
#   any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program; if not, write to the Free Software Foundation,
#   Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.
#
#   Or visit http://www.gnu.org/licenses/gpl.html.
#

__author__ = 'markus.wildi@one-arcsec.org'

import sys
import time
import re
import os
import subprocess

import rts2.scriptcomm
import rts2af 

r2c= rts2.scriptcomm.Rts2Comm()

class Acquire(rts2af.AFScript):
    """control telescope and CCD, acquire a series of focuser images and eventually set the focus"""
    def __init__(self, scriptName=None, test=None):
        self.scriptName= scriptName
        self.focuser = r2c.getValue('focuser')
        self.serviceFileOp= rts2af.serviceFileOp= rts2af.ServiceFileOperations()
        self.runTimeConfig= rts2af.runTimeConfig= rts2af.Configuration() # default config
        self.runTimeConfig.readConfiguration('/etc/rts2/rts2af/rts2af-acquire.cfg') # rts2 exe mechanism has no options
        self.testFiltersInUse=[]
        if( test==None):
            self.test= False # normal production 
        else:
            self.test= True  #True: feed rts2af_acquire.py with rts2af_feed_acquire.py
            # retrieve the list of used filters from rts2af_feed_acquire.py
            print 'filtersInUse'
            sys.stdout.flush()
            self.testFiltersInUse= sys.stdin.readline().split()            
            r2c.log('I','rts2af_acquire.py: being in test mode, filters: {0}'.format(self.testFiltersInUse))

        self.lowerLimit= self.runTimeConfig.value('FOCUSER_ABSOLUTE_LOWER_LIMIT')
        self.upperLimit= self.runTimeConfig.value('FOCUSER_ABSOLUTE_UPPER_LIMIT')
        self.speed= self.runTimeConfig.value('FOCUSER_SPEED')
        self.setFocDefFwhmUpperLimit= self.runTimeConfig.value('SET_FOC_DEF_FWHM_UPPER_THRESHOLD')
        # ToDo: read the runtime configuration of rts2-focusd-flitc not ours!
        self.temperatureCompensation= self.runTimeConfig.value('FOCUSER_TEMPERATURE_COMPENSATION')
        #r2c.log('E','rts2af_acquire: this is not the rts2-focusd-flitc, configured temperature compensation option: {0}'.format(self.temperatureCompensation)) 

        self.binning= self.runTimeConfig.value(self.runTimeConfig.ccd.binning)
        self.windowOffsetX= self.runTimeConfig.ccd.windowOffsetX
        self.windowOffsetY= self.runTimeConfig.ccd.windowOffsetY
        self.windowWidth= self.runTimeConfig.ccd.windowWidth
        self.windowHeight= self.runTimeConfig.ccd.windowHeight
        self.pid= os.getpid()


    def focPosWithinLimits(self, focPos=None):

        if( focPos < self.lowerLimit):
            r2c.log('E','rts2af_acquire: focPos: {0} below minimum: {1}'.format((focPos), self.lowerLimit)) 
            return False

        if( focPos > self.upperLimit):
            r2c.log('E','rts2af_acquire: focPos: {0} above maximum: {1}'.format((focPos), self.upperLimit)) 
            return False

        return True

    def sleepWhileFocusTravel(self, fcPos=None):
        if(not self.test):
            curFocPos= r2c.getValueFloat('FOC_POS',self.focuser)
            # let the focuser move, [speed] tick/second
            if( self.speed > 0):
                slt= 1. + abs(fcPos- curFocPos) / self.speed # ToDo, sleep a bit longer, ok?
                r2c.log('I','rts2af_acquire: sleeping for: {0} target={1} current={2}'.format(slt, fcPos, curFocPos))
                # Missouri
                #time.sleep( 45) # sleep 45 seconds
                # all others 
                time.sleep( slt)
            else:
                r2c.log('E','rts2af_acquire: focuser speed {0} <=0'.format(self.speed))

    def focuserValues(self):
        curFocPos      =r2c.getValueFloat('FOC_POS', self.focuser)
        curFocTar      =r2c.getValueFloat('FOC_TAR', self.focuser)
        curFocDef      =r2c.getValueFloat('FOC_DEF', self.focuser)
        curFocFoff     =r2c.getValueFloat('FOC_FOFF',self.focuser)
        curFocToff     =r2c.getValueFloat('FOC_TOFF',self.focuser)
        if(self.temperatureCompensation):
            curFocTc       =r2c.getValueFloat('FOC_TC',  self.focuser)
            curFocTempMeteo=r2c.getValueFloat('TEMP_METEO', self.focuser)
            curFocTcMode   =r2c.getValueFloat('TCMODE',     self.focuser)
        else:
            curFocTc       = None
            curFocTempMeteo= None
            curFocTcMode   = None

        return (curFocPos, curFocTar, curFocDef, curFocFoff, curFocToff, curFocTc, curFocTempMeteo, curFocTcMode)

    def focPosReached(self, focPos=None, focDef=None, focFoff=None): # focFoff will go away as soon as focuser works reliably
        """Sometimes the FLI focuser does not react"""

        if(not focPos==None):
            self.sleepWhileFocusTravel(fcPos=focPos)
            i= 0
            while(True):
                
                i += 1
                if(self.test):
                    r2c.log('I','rts2af_acquire: test mode current foc_pos: {0}'.format(focPos))
                    break
                else:
                    curFocPos= r2c.getValueFloat('FOC_POS',self.focuser)

                    if( abs(float(curFocPos-focPos)) < self.runTimeConfig.value('FOCUSER_RESOLUTION')):
                        r2c.log('I','rts2af_acquire: target position reached, current foc_pos: {0}, target position: {1}, resolution: {2} '.format(curFocPos, focPos, self.runTimeConfig.value('FOCUSER_RESOLUTION')))
                        break
                    elif( i == 5):
                        r2c.log('E','rts2af_acquire: target position, try again, focuser values: {0}'.format(self.focuserValues()))
                        # set value again
                        r2c.setValue('FOC_FOFF', focFoff, self.focuser)
                        r2c.setValue('FOC_DEF', focDef, self.focuser)
                        r2c.log('W','rts2af_acquire: target position again set: {0}'.format(focPos))
                    elif( i > 20):
                        r2c.log('E','rts2af_acquire: target position, breaking, could not set: {0}, current foc_pos: {1}'.format(focPos, curFocPos))
                        r2c.log('E','rts2af_acquire: target position, breaking, focuser values: {0}'.format(self.focuserValues()))
                        break

                time.sleep(1)

        else:
            r2c.log('E','rts2af_acquire: no focuser position given')

    def acquireImage(self, focDef=None, focFoff=None, exposure=None, filter=None, analysis=None, extension=None):

        if( not self.focPosWithinLimits( focDef + focFoff + filter.OffsetToClearPath)):
            r2c.log('E','rts2af_acquire: acquireImage: can not set position: {0}, out of limits'.format(focDef + focFoff + filter.OffsetToClearPath))
            return False

        r2c.setValue('exposure', exposure)

        r2c.setValue('FOC_FOFF', focFoff, self.focuser)
        self.focPosReached((focDef + focFoff + filter.OffsetToClearPath), focDef, focFoff) 

        acquisitionPath = r2c.exposure()

        # move all fits files of a given filter focus run into a separate directory 
        # in test mode the files are fetched for the original path
        storePath=self.serviceFileOp.expandToAcquisitionBasePath(filter) + acquisitionPath.split('/')[-1]
        if( not self.test):
            if( extension):
                elements= storePath.split('.fits')
                storePath= '{0}-{1}.fits'.format(elements[0], extension)

            r2c.log('I','rts2af_acquire: acquired: {0} storing at: {1}'.format(acquisitionPath, storePath))
            r2c.move(acquisitionPath, storePath)

        try:
            if(self.test):
                q_match= re.search( r'Q', acquisitionPath)
                analysis.stdin.write(acquisitionPath + '\n')

                if( not q_match==None):
                    return False
                else:
                    return True
            else: # storePath ok
                analysis.stdin.write(storePath + '\n')
                return True

        except:
            if(self.test):
                path= acquisitionPath 
            else:
                path= storePath

            r2c.log('E','rts2af_acquire: could not write to pipe: {0}'.format(path))
            return False

        return False

    def setFittedFocus(self, filter=None, analysis=None):
        # set the FOC_DEF for the clear optical path
        fwhmFocPos= -1
        r2c.log('I','rts2af_acquire: waiting for fitted focus position')

        focusLine= analysis.stdout.readline()
        r2c.log('I','rts2af_acquire: ----------------focus line: {0}'.format(focusLine))

        fwhmFocPos= None
        temperature= None
        fwhm= None
        setFocus= False
        #                            FOCUS: 3407.743919, FWHM: 2.331747,  TEMPERATURE: 9.38888931274C, OBJECTS: 20      DATAPOINTS: 14
        focusLineMatch= re.search( r'FOCUS: ([\.0-9]+), FWHM: ([\.0-9]+), TEMPERATURE: ([\-\.0-9]+)C, OBJECTS: ([0-9]+) DATAPOINTS: ([0-9]+)', focusLine)
        if( not focusLineMatch == None):
            objs= int(focusLineMatch.group(4))
            dps= int(focusLineMatch.group(5))
            if(objs > 5) and ( dps > 7): # ToDo adhoc

                fwhmFocPos= int(float(focusLineMatch.group(1)))
                r2c.log('I','rts2af_acquire: got fitted focuser position at: {0}'.format(fwhmFocPos))
                
                if(self.temperatureCompensation):
                    temperature= float(focusLineMatch.group(3))

                if( self.focPosWithinLimits( fwhmFocPos + filter.OffsetToClearPath)):
                    fwhm= float(focusLineMatch.group(2))
                    if( 0.5 < fwhm < self.setFocDefFwhmUpperLimit): # ad hoc lower limit
                        # ok
                        #
                        setFocus= True
                        r2c.log('I','rts2af_acquire: FOC_DEF set, due to FWHM={0} < {1}'.format(fwhm,self.setFocDefFwhmUpperLimit))
                    else:
                        r2c.log('W','rts2af_acquire: no FOC_DEF set, due to FWHM={0} > {1} or < 0.5 (ad hoc)'.format(fwhm,self.setFocDefFwhmUpperLimit))
                else:
                    r2c.log('E','rts2af_acquire: can not set FOC_DEF: {0}, due the sum FOC_DEF + filter.OffsetToClearPath= {1}, out of limits'.format(fwhmFocPos, fwhmFocPos + filter.OffsetToClearPath))
            else:
                r2c.log('E','rts2af_acquire: can not set FOC_DEF: {0}, due number of objects {1}<6 or datapoints <8 (adhoc!)'.format(fwhmFocPos, objs, dps))
        else:
            # if there is no temperature all subsequent settings of FOC will be bad positions!
            r2c.log('E','rts2af_acquire: severe error, no match for string: {0}'.format(focusLine))

        if setFocus:
            # not necessary but for run time safety, 
            # loosing time in prepareAcquisition, because travelling needs: filter.OffsetToClearPath/(focuser speed) seconds
            # this are typically 15 seconds!
            r2c.setValue('FOC_TOFF', 0, self.focuser)
            r2c.setValue('FOC_FOFF', 0, self.focuser)
            if(self.temperatureCompensation):
                r2c.setValue('TC_TEMP_REF', temperature, self.focuser)
                r2c.log('I','rts2af_acquire: setting temperature: {0}'.format(temperature))
                # set first mode to avoid caculation of TC Offset in focuser driver
                r2c.setValue('TCMODE', 1, self.focuser) # relative temperature compensation
                r2c.log('I','rts2af_acquire: setting mode to relative temperature compensation')
                modeRelative= r2c.getValue('TCMODE', self.focuser)
                r2c.log('I','rts2af_acquire: temperature compensation mode: {0}'.format(modeRelative))
            else:
                r2c.log('I','rts2af_acquire: no temperature comensation mode set, due to focuser_temperature_compensation==False')
                    
            r2c.setValue('FOC_DEF', fwhmFocPos, self.focuser)
            self.focPosReached(fwhmFocPos, fwhmFocPos, 0)
            return fwhmFocPos
        else:
            if(self.temperatureCompensation):
                r2c.setValue('TCMODE', 1, self.focuser) # relative temperature compensation
            else:
                r2c.log('I','rts2af_acquire: no temperature comensation mode set, due to focuser_temperature_compensation==False')
            return None

    def prepareAcquisition(self, focDef, filter):
        r2c.setValue('SHUTTER', 'LIGHT')
        r2c.setValue('binning', self.binning)
        r2c.setValue('WINDOW','%d %d %d %d' % (self.windowOffsetX, self.windowOffsetY, self.windowWidth, self.windowHeight))

        if(self.temperatureCompensation):

            if( not self.test):
                r2c.setValue('TCMODE', 2, self.focuser) # no temperature compensation
                time.sleep(1)
                tcMode= r2c.getValue('TCMODE', self.focuser)

                if(tcMode ==2): # ToDo 
                    r2c.log('E','rts2af_acquire: temperature compensation set to: {0} (none)'.format(tcMode))
                else:
                    r2c.log('E','rts2af_acquire: temperature compensation not set to none, it is: {0}'.format(tcMode))

        curFocPos=-1
        if( not self.test):
            curFocPos= r2c.getValueFloat('FOC_POS',self.focuser)
            r2c.log('I','rts2af_acquire: prepareAcquisition: current focuser position: {0}'.format(curFocPos))

        if(( self.focPosWithinLimits( focDef + filter.OffsetToClearPath + filter.lowerLimit)) and( self.focPosWithinLimits( focDef + filter.OffsetToClearPath + filter.upperLimit))):
            # the order is important
            r2c.setValue('FOC_FOFF' , 0, self.focuser)
            # ToDo: verify: triggers setting of filter offset as FOC_TOFF as defined in the rts2 devices file
            # 2011-05-28: a manual filter wheel setting does not trigger the offset.
            r2c.setValue('filter', filter.name)
            #ToDo: remove if above is true
            r2c.setValue('FOC_TOFF', filter.OffsetToClearPath, self.focuser)
            # if True, meaning: use configured value
            if(self.runTimeConfig.value('SET_INIIAL_FOC_DEF')):
                r2c.setValue('FOC_DEF', focDef, self.focuser)
                r2c.log('I','rts2af_acquire: prepareAcquisition: setting FOC_DEF: {0}'.format(focDef))
                self.focPosReached((focDef + filter.OffsetToClearPath), focDef, 0)
            else:
                r2c.log('I','rts2af_acquire: prepareAcquisition: not setting FOC_DEF (see configuration)')

            return True
        else:
            r2c.log('E','rts2af_acquire: prepareAcquisition: can not set position: lower: {0}, upper: {1}, out of limits: {2}, {3}'.format((focDef + filter.OffsetToClearPath + filter.lowerLimit), (focDef + filter.OffsetToClearPath + filter.upperLimit),self.lowerLimit , self.upperLimit))
            
            r2c.setValue('FOC_TOFF', 0, self.focuser)
            r2c.setValue('FOC_FOFF', 0, self.focuser)
            r2c.log('I','rts2af_acquire: prepareAcquisition: set FOC_TOFF=FOC_FOFF=0')
            return False
#ToDo
    def saveState(self):
        return

    def run(self):

        r2c.log('I','rts2af_acquire: starting')
        exposureTime= 0
        startTime= time.time()
        # telescope parameter
        telescope=rts2af.Telescope() # take the defaults from the config file or overwrite them here

        # start analysis process
        analysis={}

        # create the result file for the fitted values, read by rts2af_set_fit_focus.py
        fitResultFileName= self.serviceFileOp.expandToFitResultPath( 'rts2af-acquire-')
        with open( fitResultFileName, 'w') as frfn:
            frfn.write('# {0}, written by rts2af_acquire.py\n#\n'.format(fitResultFileName))
        # rts2af_acquire.py will supply the values asynchronously
        frfn.close()

        filtersInUse=[]
        if(self.test):
            filtersInUse= self.testFiltersInUse
        else:
            filtersInUse= self.runTimeConfig.filtersInUse

        fwhm_foc_pos_fit= -1
        focDef= -1
        for fltName in filtersInUse:
            filterExposureTime= 0
            filterStartTime=time.time()
            filter= self.runTimeConfig.filterByName( fltName)
            try:
                r2c.log('I','rts2af_acquire: Initial setting: filter: {0}'.format(filter.name, self.runTimeConfig.configurationFileName()))
            except:
                r2c.log('E','rts2af_acquire: no filter configuration found for  filter: {0} in {0}'.format(fltName, self.runTimeConfig.configurationFileName()))
            if( fwhm_foc_pos_fit > 0):
                focDef= fwhm_foc_pos_fit
                self.prepareAcquisition( focDef, filter) # a previous run was successful
                r2c.log('I','Initial setting: filter: {0}, offset: {1}, expousre: {2} (setting from clear path run)'.format( filter.name, fwhm_foc_pos_fit, filter.exposure))
            else:

                if( self.runTimeConfig.value('SET_INIIAL_FOC_DEF')):

                    focDef= self.runTimeConfig.value('DEFAULT_FOC_POS')
                else:
                    if( not self.test):
                        focDef= r2c.getValueFloat('FOC_DEF',self.focuser)
                    else:
                        focDef= self.runTimeConfig.value('DEFAULT_FOC_POS')


                if( self.prepareAcquisition( focDef, filter)): 
                    r2c.log('I','rts2af_acquire: Initial setting: foc_def: {0}, filter: {1}, offset: {2}, expousre: {3}'.format( focDef, filter.name, filter.OffsetToClearPath, filter.exposure))

                else:
                    r2c.log('I','rts2af_acquire: something went wrong in prepareAcquisition, continue with next filter')
                    continue # something went wrong

            configFileName= self.serviceFileOp.expandToTmpConfigurationPath( 'rts2af-acquire-' + filter.name + '-') 

            self.runTimeConfig.writeConfigurationForFilter(configFileName, fltName)
            self.serviceFileOp.createAcquisitionBasePath( filter)
# ToDo wildi
            if( self.test):
                cmd= [ '/home/wildi/rts2/scripts/rts2af/rts2af_feedback_acquire.py']
            else:
                cmd= [ '/home/wildi/rts2/scripts/rts2af/rts2af_analysis.py',
                       '--config', configFileName
                       ]
            
            r2c.log('I','rts2af_acquire: pid: {0}, start for COMMAND: {1}, filter: {2}'.format(self.pid, cmd, filter.name))
            # open the analysis suprocess
            try:
                analysis[filter.name] = subprocess.Popen( cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            except:
                r2c.log('E','rts2af_acquire: exiting, could not start: {0}, filter: {1}, position: {2}, exposure: {3}'.format( cmd, filter.name, filter.OffsetToClearPath, filter.exposure))
                sys.exit(1)

            # create the reference catalogue
            if( not self.acquireImage( focDef, 0, filter.exposure, filter, analysis[filter.name], 'reference')): # exposure depends on position
                msg= analysis[filter.name].stdout.readline()
                r2c.log('E','rts2af_acquire: received from pipe: {0}'.format(msg))
                r2c.log('I','rts2af_acquire: continue with next filter')
                continue # something went wrong

            else:
                filterExposureTime += filter.exposure
                msgRaw= analysis[filter.name].stdout.readline()
                msg= re.split('\n', msgRaw)
                r2c.log('E','rts2af_acquire: received from pipe: {0}'.format(msg[0]))
                if( msg[0] == 'FOCUS: -1'):  # check creation of reference catalogue
                    r2c.log('I','rts2af_acquire: continue with next filter')
                    continue # something went wrong                                                                                                  
            if(self.test):
                while( True):
                    r2c.log('I','rts2af_acquire: being in test mode, filter: {0}, offset: {1}, exposure: {2}'.format(focDef, filter.OffsetToClearPath, filter.exposure))
                    filterExposureTime += filter.exposure
                    if( not self.acquireImage( focDef, filter.OffsetToClearPath, filter.exposure, filter, analysis[filter.name], None)):
                        break # exhausted

                r2c.log('I','rts2af_acquire: focuser exposures finished for filter: {0}'.format(filter.name))
                if(filter.OffsetToClearPath== 0):
                    if( self.runTimeConfig.value('SET_FOCUS')):
                        fwhm_foc_pos_fit= self.setFittedFocus(filter, analysis[filter.name])
                    else:
                        r2c.log('I','rts2af_acquire: not attempting to set focus (see configuration)')

                else:
                    r2c.log('I','rts2af_acquire: not setting fit results for filter: {0}, not clear path'.format(filter.name))
            else:
                # loop over the focuser steps
                for setting in filter.settings:
                    #if(( abs(setting.offset) < 410) or ( abs(setting.offset)==720)): # to reduce the time, ToDo: a true general solution
                    exposure=  telescope.linearExposureTimeAtFocPos(setting.exposure, setting.offset)
                    r2c.log('I','rts2af_acquire: filter: {0}, offset: {1}, exposure: {2}, true exposure: {3}'.format(filter.name, setting.offset, setting.exposure, exposure))
                    filterExposureTime += exposure
                    if( not self.acquireImage( focDef, setting.offset, exposure, filter, analysis[filter.name], None)):
                        r2c.log('E','rts2af_acquire: breaking for filter: {0}'.format(filter.name))
                        break # could not write to pipe (analysis process died)
                else: # exhausted
                    # signal rts2af_analysis.py to continue with fitting
                    analysis[filter.name].stdin.write('Q\n')
                    r2c.log('I','rts2af_acquire: focuser exposures finished for filter: {0}'.format(filter.name))

                    if(filter.OffsetToClearPath== 0):
                        if( self.runTimeConfig.value('SET_FOCUS')):
                            fwhm_foc_pos_fit= self.setFittedFocus(filter, analysis[filter.name])
                            if( fwhm_foc_pos_fit):
                                # take a proof
                                if( self.acquireImage( fwhm_foc_pos_fit, 0, filter.exposure, filter, analysis[filter.name], 'proof')):
                                    r2c.log('I','rts2af_acquire: proof taken at FOC_POS:{0}'.format(fwhm_foc_pos_fit))
                                else:
                                    r2c.log('I','rts2af_acquire: could not take a proof at FOC_POS:{0}'.format(fwhm_foc_pos_fit))
                            else:
                                r2c.log('E','rts2af_acquire: something went wrong within setFittedFocus')
                        else:
                            r2c.log('I','rts2af_acquire: not attempting to set focus (see configuration)')
                    else:
                        r2c.log('I','rts2af_acquire: not setting fit results for filter: {0}, not clear path'.format(filter.name))

            exposureTime += filterExposureTime 
            filterEndTime=time.time()
            r2c.log('I','rts2af_acquire: pid: {0}, ended within: {1} seconds, accumulated exposure time: {2}, for COMMAND: {3}, filter: {4}'.format(self.pid, (filterEndTime-filterStartTime), filterExposureTime, cmd, filter.name))

        # completed
        endTime= time.time()
        r2c.log('I','rts2af_acquire: focuser exposures finished for all filters within: {0} seconds, accumulated exposure time: {1}'.format((endTime- startTime), exposureTime))


        return

if __name__ == '__main__':
# if any extra argument is present then rts2af_acquire.py is executed in test mode
    if( len(sys.argv)== 2):
        acquire= Acquire(scriptName=sys.argv[0], test=sys.argv[1])
    else:
        acquire= Acquire(scriptName=sys.argv[0])

    acquire.run()