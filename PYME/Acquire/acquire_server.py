#!/usr/bin/python

##################
# smimainframe.py
#
# Copyright David Baddeley, 2009
# d.baddeley@auckland.ac.nz
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##################
"""
This contains the bulk of the GUI code for the main window of PYMEAcquire.
"""
import logging
import os
import time

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

from PYME.Acquire import microscope
from PYME.Acquire import protocol

from PYME.IO import MetaDataHandler
import six

from PYME.util import webframework
import threading


class PYMEAcquireServer(object):
    def __init__(self, options = None):
        self.options = options

        self.snapNum = 0
        
        self.MainFrame = self #reference to this window for use in scripts etc...
        protocol.MainFrame = self

        self.initDone = False

        self.postInit = [] #for protocol compat

        self.scope = microscope.microscope()

        self.roi_on = False
        self.bin_on = False
        
        #functions to call in each polling iteration
        # Replaces time1 in GUI version
        self._want_loop_notification = []
        
        self._is_running = False
        
        # variables to facilitate long-polling for frame updates
        self._current_frame = None
        self._new_frame_condition = threading.Condition()
        
        self._state_valid = False
        self._state_updated_condition = threading.Condition()
        
        self.scope.state.stateChanged.connect(self._on_scope_state_change)
        
        
        
        
    def main_loop(self):
        """
        Infinite loop which polls hardware
        
        Returns
        -------

        """
        self._is_running = True

        self._initialize_hardware()
        #poll to see if the init script has run
        self._wait_for_init_complete()
        
        
        logger.debug('Starting post-init')

        if self.scope.cam.CamReady():# and ('chaninfo' in self.scope.__dict__)):
            self._start_polling_camera()

        self._want_loop_notification.append(self.scope.actions.Tick)
        self.initDone = True

        logger.debug('Finished post-init')
        
        while self._is_running:
            for fcn in self._want_loop_notification:
                fcn()
            
            # 100 ms refresh rate
            time.sleep(0.1)
            
        self._shutdown()
        
    def stop(self):
        self._is_running = False


    def _initialize_hardware(self):
        """
        Launch microscope hardware initialization and start polling for completion

        """
        #this spawns a new thread to run the initialization script
        self.scope.initialize(self.options.initFile, self.__dict__)

        logger.debug('Init run, waiting on background threads')

    def _wait_for_init_complete(self):
        while not self.scope.initDone:
            time.sleep(0.1)
            
        #if self.scope.initDone == True:
        logger.debug('Backround initialization done')
        
    def _on_frame_group(self, *args, **kwargs):
        with self._new_frame_condition:
            self._current_frame = self.scope.frameWrangler.currentFrame
            self._new_frame_condition.notify()
            
    def _on_scope_state_change(self, *args, **kwargs):
        with self._state_updated_condition:
            self._state_valid = False
            self._state_updated_condition.notify()
    
    def _start_polling_camera(self):
        self.scope.startFrameWrangler()
        self.scope.frameWrangler.onFrameGroup.connect(self._on_frame_group)
        
    @webframework.register_endpoint('/get_frame_pzf', mimetype='image/pzf')
    def get_frame_pzf(self):
        """
        Get a frame in PZF format (compressed, fast), uses long polling
        
        Returns
        -------

        """
        from PYME.IO import PZFFormat
        with self._new_frame_condition:
            while not self._current_frame is None:
                self._new_frame_condition.wait()
                
            ret = PZFFormat.dumps(self._current_frame, compression=PZFFormat.DATA_COMP_HUFFCODE)
            self._current_frame = None
            
        return ret
    
    @webframework.register_endpoint('/get_frame_png', mimetype='image/png')
    def get_frame_png(self):
        """
        Get a frame in PNG format
        
        uses long polling
        
        Returns
        -------

        """
        import numpy as np
        from io import BytesIO
        from PIL import Image

        out = BytesIO()
        
        with self._new_frame_condition:
            while not self._current_frame is None:
                self._new_frame_condition.wait()

            im = np.sqrt(self._current_frame).astype('uint8')
            
            #im = self._current_frame - self._current_frame.min()
            #im = (255*im/im.max()).astype('uint8')

            Image.fromarray(im.T).save(out, 'PNG')
            self._current_frame = None
            
        s = out.getvalue()
        out.close()
        return s
        
    @webframework.register_endpoint('/get_scope_state', output_is_json=False)
    def get_scope_state(self, keys=None):
        """
        Gets the current scope state as a json dictionary
        
        Parameters
        ----------
        keys : list, optional
          a list of keys to interrogate. If none, returns full state.

        Returns
        -------

        """
        
        if keys is None:
            keys = self.scope.state.keys()
            
        return {k : self.scope.state[k] for k in keys}

    @webframework.register_endpoint('/scope_state_longpoll', output_is_json=False)
    def scope_state_longpoll(self, keys=None):
        """
        Gets the current scope state as a json dictionary, only returning once the state has changed

        Parameters
        ----------
        keys : list, optional
          a list of keys to interrogate. If none, returns full state.

        Returns
        -------

        """
        if keys is None:
            keys = self.scope.state.keys()
            
        with self._state_updated_condition:
            while self._state_valid:
                self._state_updated_condition.wait()
    
            ret = {k: self.scope.state[k] for k in keys}
            self._state_valid = True
    
        return ret
    
    @webframework.register_endpoint('/update_scope_state')
    def update_scope_state(self, body=''):
        import json
        state = json.loads(body)
        
        self.scope.state.update(state)
        
        return 'OK' #TODO - check for errors


    def OnMCamSetPixelSize(self, event):
        from PYME.Acquire.ui import voxelSizeDialog

        dlg = voxelSizeDialog.VoxelSizeDialog(self, self.scope)
        dlg.ShowModal()



    def _shutdown(self):
        self.scope.frameWrangler.stop()
        
        if 'cameras' in dir(self.scope):
            for c in self.scope.cameras.values():
                c.Shutdown()
        else:
            self.scope.cam.Shutdown()
            
        for f in self.scope.CleanupFunctions:
            f()
            
        logger.info('All cleanup functions called')
        
        time.sleep(1)
        
        import threading
        msg = 'Remaining Threads:\n'
        for t in threading.enumerate():
            if six.PY3:
                msg += '%s, %s\n' % (t.name, t._target)
            else:
                msg += '%s, %s\n' % (t, t._Thread__target)
            
        logger.info(msg)


class AcquireHTTPServer(webframework.APIHTTPServer, PYMEAcquireServer):
    """
    Combines the RuleServer with it's web framework.

    Largely an artifact of initial experiments using cherrypy (allowed quickly switching between cherrypy
    and our internal webframework).
    """
    
    def __init__(self, options, port, bind_addr=''):
        PYMEAcquireServer.__init__(self, options)
        
        server_address = (bind_addr, port)
        webframework.APIHTTPServer.__init__(self, server_address)
        self.daemon_threads = True
        
    def run(self):
        self._poll_thread = threading.Thread(target=self.main_loop)
        self._poll_thread.start()
        
        try:
            self.serve_forever()
        finally:
            self.stop()
            #logger.info('Shutting down ...')
            #self.distributor.shutdown()
            logger.info('Closing server ...')
            self.server_close()


def main():
    import os
    import sys
    from optparse import OptionParser
    logging.basicConfig(level=logging.DEBUG)
    
    from PYME import config
    
    logger = logging.getLogger(__name__)
    parser = OptionParser()
    parser.add_option("-i", "--init-file", dest="initFile",
                      help="Read initialisation from file [defaults to init.py]",
                      metavar="FILE", default='init.py')
    
    (options, args) = parser.parse_args()
    
    # continue to support loading scripts from the PYMEAcquire/Scripts directory
    legacy_scripts_dir = os.path.join(os.path.dirname(__file__), 'Scripts')
    
    # use new config module to locate the initialization file
    init_file = config.get_init_filename(options.initFile, legacy_scripts_directory=legacy_scripts_dir)
    if init_file is None:
        logger.critical('init script %s not found - aborting' % options.initFile)
        sys.exit(1)
    
    #overwrite initFile in options with full path - CHECKME - does this work?
    options.initFile = init_file
    logger.info('using initialization script %s' % init_file)
    
    server = AcquireHTTPServer(options, 8999)
    server.run()
    


if __name__ == '__main__':
    from PYME.util import mProfile, fProfile
    
    #mProfile.profileOn(['acquiremainframe.py', 'microscope.py', 'frameWrangler.py', 'fakeCam.py', 'rend_im.py'])
    #fp = fProfile.thread_profiler()
    #fp.profileOn()
    main()
    #fp.profileOff()
    #mProfile.report()