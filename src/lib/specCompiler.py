import os, sys
import re
import time
import math
import subprocess
import numpy
import itertools
import glob

from multiprocessing import Pool

sys.path.append("lib")
sys.path.append(os.path.join("lib","cores"))

import project
import regions
import parseLP
from createJTLVinput import createLTLfile, createSMVfile, createTopologyFragment, createInitialRegionFragment
from parseEnglishToLTL import bitEncoding, replaceRegionName, createStayFormula
import fsa
from copy import deepcopy
from coreUtils import *

# Hack needed to ensure there's only one
_SLURP_SPEC_GENERATOR = None


class SpecCompiler(object):
    def __init__(self, spec_filename=None):
        self.proj = project.Project()

        if spec_filename is not None:
            self.loadSpec(spec_filename)

    def loadSpec(self,spec_filename):
        """
        Load the project object
        """
        self.proj.loadProject(spec_filename)

        # Check to make sure this project is complete
        if self.proj.rfi is None:
            print "ERROR: Please define regions before compiling."
            return
    
        # Remove comments
        self.specText = re.sub(r"#.*$", "", self.proj.specText, flags=re.MULTILINE)

        if self.specText.strip() == "":
            print "ERROR: Please write a specification before compiling."
            return

    def loadSimpleSpec(self,text="", regionList=[], sensors=[], actuators=[], customs=[], adj=[], outputfile=""):
        """
        Load a simple spec given by the arguments without reading from a spec file
        
        For Slurp

        region, sensors, actuators, customs are lists of strings representing props
        adj is a list of tuples [(region1,region2),...]
        """

        if outputfile == "":
            print "need to specify output filename"
            return

        self.proj.compile_options['decompose'] = False
        self.proj.project_root = os.path.abspath(os.path.dirname(os.path.expanduser(outputfile)))
        self.proj.project_basename, ext = os.path.splitext(os.path.basename(outputfile))
        self.proj.specText=text
        # construct a list of region objects with given names
        self.proj.rfi = regions.RegionFileInterface()
        for rname in regionList:
            self.proj.rfi.regions.append(regions.Region(name=rname))

        self.proj.enabled_sensors = sensors
        self.proj.enabled_actuators = actuators
        self.proj.all_customs = customs

        # construct adjacency matrix
        self.proj.rfi.transitions= [[[] for j in range(len(self.proj.rfi.regions))] for i in range(len(self.proj.rfi.regions))]
        for tran in adj:
            idx0 = self.proj.rfi.indexOfRegionWithName(tran[0])
            idx1 = self.proj.rfi.indexOfRegionWithName(tran[1])
            self.proj.rfi.transitions[idx0][idx1] = [(0,0)] # fake trans face
            self.proj.rfi.transitions[idx1][idx0] = [(0,0)]

    def _decompose(self):
        self.parser = parseLP.parseLP()
        self.parser.main(self.proj.getFilenamePrefix() + ".spec")

        # Remove all references to any obstacle regions at this point
        for r in self.proj.rfi.regions:
            if r.isObstacle:
                # Delete corresponding decomposed regions
                for sub_r in self.parser.proj.regionMapping[r.name]:
                    del self.parser.proj.rfi.regions[self.parser.proj.rfi.indexOfRegionWithName(sub_r)]

                    # Remove decomposed region from any overlapping mappings
                    for k,v in self.parser.proj.regionMapping.iteritems(): 
                        if k == r.name: continue
                        if sub_r in v:
                            v.remove(sub_r)

                # Remove mapping for the obstacle region
                del self.parser.proj.regionMapping[r.name]

        #self.proj.rfi.regions = filter(lambda r: not (r.isObstacle or r.name == "boundary"), self.proj.rfi.regions)
                    
        # save the regions into new region file
        filename = self.proj.getFilenamePrefix() + '_decomposed.regions'
        self.parser.proj.rfi.recalcAdjacency()
        self.parser.proj.rfi.writeFile(filename)


        self.proj.regionMapping = self.parser.proj.regionMapping
        self.proj.writeSpecFile()
        
    def _writeSMVFile(self):
        if self.proj.compile_options["decompose"]:
            numRegions = len(self.parser.proj.rfi.regions)
        else:
            numRegions = len(self.proj.rfi.regions)
        sensorList = self.proj.enabled_sensors
        robotPropList = self.proj.enabled_actuators + self.proj.all_customs + self.proj.internal_props

        # Add in regions as robot outputs
        if self.proj.compile_options["use_region_bit_encoding"]:
            robotPropList.extend(["bit"+str(i) for i in range(0,int(numpy.ceil(numpy.log2(numRegions))))])
        else:
            robotPropList.extend([r.name for r in self.parser.proj.rfi.regions])

        self.propList = sensorList + robotPropList
        
        createSMVfile(self.proj.getFilenamePrefix(), sensorList, robotPropList)

    def _writeLTLFile(self):

        self.LTL2SpecLineNumber = None

        #regionList = [r.name for r in self.parser.proj.rfi.regions]
        regionList = [r.name for r in self.proj.rfi.regions]
        sensorList = deepcopy(self.proj.enabled_sensors)
        robotPropList = self.proj.enabled_actuators + self.proj.all_customs
        
        text = self.proj.specText

        response = None

        # Create LTL using selected parser
        # TODO: rename decomposition object to something other than 'parser'
        if self.proj.compile_options["parser"] == "slurp":
            # default to no region tags if no simconfig is defined, so we can compile without
            if self.proj.currentConfig is None:
                region_tags = {}
            else:
                region_tags = self.proj.currentConfig.region_tags
 
            # Hack: We need to make sure there's only one of these
            global _SLURP_SPEC_GENERATOR
            
            # Make a new specgenerator and have it process the text
            if not _SLURP_SPEC_GENERATOR:
                # Add SLURP to path for import
                p = os.path.dirname(os.path.abspath(__file__))
                sys.path.append(os.path.join(p, "..", "etc", "SLURP"))
                from ltlbroom.specgeneration import SpecGenerator
                _SLURP_SPEC_GENERATOR = SpecGenerator()
            
            # Filter out regions it shouldn't know about
            filtered_regions = [region.name for region in self.proj.rfi.regions 
                                if not (region.isObstacle or region.name.lower() == "boundary")]
            LTLspec_env, LTLspec_sys, self.proj.internal_props, internal_sensors, results, responses, traceback = \
                _SLURP_SPEC_GENERATOR.generate(text, sensorList, filtered_regions, robotPropList, region_tags)

            oldspec_env = LTLspec_env
            oldspec_sys = LTLspec_sys
 
            for ln, result in enumerate(results):
                if not result:
                    print "WARNING: Could not parse the sentence in line {0}".format(ln)

            # Abort compilation if there were any errors
            if not all(results):
                return None, None, responses
        
            # Add in the sensors so they go into the SMV and spec files
            for s in internal_sensors:
                if s not in sensorList:
                    sensorList.append(s)
                    self.proj.all_sensors.append(s)
                    self.proj.enabled_sensors.append(s)                    

            # Conjoin all the spec chunks
            LTLspec_env = '\t\t' + ' & \n\t\t'.join(LTLspec_env)
            LTLspec_sys = '\t\t' + ' & \n\t\t'.join(LTLspec_sys)
            
            if self.proj.compile_options["decompose"]:
                # substitute decomposed region names
                for r in self.proj.rfi.regions:
                    if not (r.isObstacle or r.name.lower() == "boundary"):
                        LTLspec_env = re.sub('\\bs\.' + r.name + '\\b', "("+' | '.join(["s."+x for x in self.parser.proj.regionMapping[r.name]])+")", LTLspec_env)
                        LTLspec_env = re.sub('\\be\.' + r.name + '\\b', "("+' | '.join(["e."+x for x in self.parser.proj.regionMapping[r.name]])+")", LTLspec_env)
                        LTLspec_sys = re.sub('\\bs\.' + r.name + '\\b', "("+' | '.join(["s."+x for x in self.parser.proj.regionMapping[r.name]])+")", LTLspec_sys)
                        LTLspec_sys = re.sub('\\be\.' + r.name + '\\b', "("+' | '.join(["e."+x for x in self.parser.proj.regionMapping[r.name]])+")", LTLspec_sys)

            response = responses

        elif self.proj.compile_options["parser"] == "ltl":
            # delete comments
            text = re.sub(r"#.*$", "", text, flags=re.MULTILINE)

            # split into env and sys parts (by looking for a line of just dashes in between)
            LTLspec_env, LTLspec_sys = re.split(r"^\s*-+\s*$", text, maxsplit=1, flags=re.MULTILINE)

            # split into subformulas
            LTLspec_env = re.split(r"(?:[ \t]*[\n\r][ \t]*)+", LTLspec_env)
            LTLspec_sys = re.split(r"(?:[ \t]*[\n\r][ \t]*)+", LTLspec_sys)

            # remove any empty initial entries (HACK?)
            while '' in LTLspec_env:
                LTLspec_env.remove('')
            while '' in LTLspec_sys:
                LTLspec_sys.remove('')

            print LTLspec_env
            print LTLspec_sys

            # automatically conjoin all the subformulas
            LTLspec_env = '\t\t' + ' & \n\t\t'.join(LTLspec_env)
            LTLspec_sys = '\t\t' + ' & \n\t\t'.join(LTLspec_sys)

            # substitute decomposed region 
            for r in self.proj.rfi.regions:
                if not (r.isObstacle or r.name.lower() == "boundary"):
                    LTLspec_env = re.sub('\\b' + r.name + '\\b', "("+' | '.join(["s."+x for x in self.parser.proj.regionMapping[r.name]])+")", LTLspec_env)
                    LTLspec_sys = re.sub('\\b' + r.name + '\\b', "("+' | '.join(["s."+x for x in self.parser.proj.regionMapping[r.name]])+")", LTLspec_sys)

            traceback = [] # HACK: needs to be something other than None
        elif self.proj.compile_options["parser"] == "structured":
            import parseEnglishToLTL

            # substitute decomposed region 
            for r in self.proj.rfi.regions:
                if not (r.isObstacle or r.name.lower() == "boundary"):
                    text = re.sub('\\b' + r.name + '\\b', "("+' | '.join(["s."+x for x in self.parser.proj.regionMapping[r.name]])+")", text)

            regionList = ["s."+x.name for x in self.parser.proj.rfi.regions]

            spec, traceback, failed, self.LTL2SpecLineNumber = parseEnglishToLTL.writeSpec(text, sensorList, regionList, robotPropList)

            # Abort compilation if there were any errors
            if failed:
                return None, None, None

            LTLspec_env = spec["EnvInit"] + spec["EnvTrans"] + spec["EnvGoals"]
            LTLspec_sys = spec["SysInit"] + spec["SysTrans"] + spec["SysGoals"]

            # HACK: account for the []<>TRUE goal we are adding
            traceback['SysGoals'].insert(0, None)
        else:
            print "Parser type '{0}' not currently supported".format(self.proj.compile_options["parser"])
            return None, None, None

        # Prepend "e." or "s." to propositions for JTLV
        for i, sensor in enumerate(sensorList):
            text = re.sub("\\b"+sensor+"\\b", "e." + sensor, text)
            sensorList[i] = "e." + sensorList[i]

        for i, prop in enumerate(robotPropList):
            text = re.sub("\\b"+prop+"\\b", "s." + prop, text)
            robotPropList[i] = "s." + robotPropList[i]

        regionList = [x.name for x in self.parser.proj.rfi.regions]

        if self.proj.compile_options["use_region_bit_encoding"]:
            # Define the number of bits needed to encode the regions
            numBits = int(math.ceil(math.log(len(regionList),2)))

            # creating the region bit encoding
            bitEncode = bitEncoding(len(regionList),numBits)
            currBitEnc = bitEncode['current']
            nextBitEnc = bitEncode['next']

            # switch to bit encodings for regions
            LTLspec_env = replaceRegionName(LTLspec_env, bitEncode, regionList)
            LTLspec_sys = replaceRegionName(LTLspec_sys, bitEncode, regionList)
        
            if self.LTL2SpecLineNumber is not None:
                for k in self.LTL2SpecLineNumber.keys():
                    new_k = replaceRegionName(k, bitEncode, regionList)
                    if new_k != k:
                        self.LTL2SpecLineNumber[new_k] = self.LTL2SpecLineNumber[k]
                        del self.LTL2SpecLineNumber[k]

        if self.proj.compile_options["decompose"]:
            adjData = self.parser.proj.rfi.transitions
        else:
            adjData = self.proj.rfi.transitions

        # Store some data needed for later analysis
        self.spec = {}
        self.spec['Topo'] = createTopologyFragment(adjData, self.parser.proj.rfi.regions, use_bits=self.proj.compile_options["use_region_bit_encoding"])

        # Substitute any macros that the parsers passed us
        LTLspec_env = self.substituteMacros(LTLspec_env)
        LTLspec_sys = self.substituteMacros(LTLspec_sys)

        # Add in a fragment to make sure that we start in a valid region
        LTLspec_sys += "\n&\n" + createInitialRegionFragment(self.parser.proj.rfi.regions, use_bits=self.proj.compile_options["use_region_bit_encoding"])
        
        # If we are not using bit-encoding, we need to
        # explicitly encode a mutex for regions
        if not self.proj.compile_options["use_region_bit_encoding"]:
            # DNF version (extremely slow for core-finding)
            #mutex = "\n\t&\n\t []({})".format(" | ".join(["({})".format(" & ".join(["s."+r2.name if r is r2 else "!s."+r2.name for r2 in self.parser.proj.rfi.regions])) for r in self.parser.proj.rfi.regions]))

            # Almost-CNF version
            exclusions = []
            for i, r1 in enumerate(self.parser.proj.rfi.regions):
                for r2 in self.parser.proj.rfi.regions[i+1:]:
                    exclusions.append("!(s.{} & s.{})".format(r1.name, r2.name))
            mutex = "\n&\n\t []({})".format(" & ".join(exclusions))
            LTLspec_sys += mutex

        self.spec.update(self.splitSpecIntoComponents(LTLspec_env, LTLspec_sys))

        LTLspec_sys += "\n&\n" + self.spec['Topo']

        createLTLfile(self.proj.getFilenamePrefix(), LTLspec_env, LTLspec_sys)
        
        if self.proj.compile_options["parser"] == "slurp":
            self.reversemapping = {self.postprocessLTL(line,sensorList,robotPropList).strip():line.strip() for line in oldspec_env + oldspec_sys}
            self.reversemapping[self.spec['Topo'].replace("\n","").replace("\t","").lstrip().rstrip("\n\t &")] = "TOPOLOGY"

        #for k,v in self.reversemapping.iteritems():
        #    print "{!r}:{!r}".format(k,v)        

        return self.spec, traceback, response

    def substituteMacros(self, text):
        """
        Replace any macros passed to us by the parser.  In general, this is only necessary in cases
        where bitX propositions are needed, since the parser is not supposed to know about them.
        """

        # This creates a mirrored copy of topological constraints for the target we are following
        if "FOLLOW_SENSOR_CONSTRAINTS" in text:
            if not self.proj.compile_options["use_region_bit_encoding"]:
                print "WARNING: currently, bit encoding must be enabled for follow sensor"
            else:
                env_topology = self.spec['Topo'].replace("s.bit", "e.sbit")
                initreg_formula = createInitialRegionFragment(self.parser.proj.rfi.regions, use_bits=True).replace("s.bit", "e.sbit")

                sensorBits = ["sbit{}".format(i) for i in range(0,int(numpy.ceil(numpy.log2(len(self.parser.proj.rfi.regions)))))]
                for p in sensorBits:
                    if p not in self.proj.enabled_sensors:
                        self.proj.enabled_sensors.append(p)
                    if p not in self.proj.all_sensors:   
                        self.proj.all_sensors.append(p)

                text = text.replace("FOLLOW_SENSOR_CONSTRAINTS", env_topology + "\n&\n" + initreg_formula)

        # Stay-there macros.  This can be done using just region names, but is much more concise
        # using bit encodings (and thus much faster to encode as CNF)

        if "STAY_THERE" in text:
            text = text.replace("STAY_THERE", createStayFormula([r.name for r in self.parser.proj.rfi.regions], use_bits=self.proj.compile_options["use_region_bit_encoding"]))

        if "TARGET_IS_STATIONARY" in text:
            text = text.replace("TARGET_IS_STATIONARY", createStayFormula([r.name for r in self.parser.proj.rfi.regions], use_bits=self.proj.compile_options["use_region_bit_encoding"]).replace("s.","e.s"))

        return text

    
    def postprocessLTL(self, text, sensorList, robotPropList):
        # TODO: make everything use this
        if self.proj.compile_options["decompose"]:
            # substitute decomposed region names
            for r in self.proj.rfi.regions:
                if not (r.isObstacle or r.name.lower() == "boundary"):
                    text = re.sub('\\bs\.' + r.name + '\\b', "("+' | '.join(["s."+x for x in self.parser.proj.regionMapping[r.name]])+")", text)
                    text = re.sub('\\be\.' + r.name + '\\b', "("+' | '.join(["e."+x for x in self.parser.proj.regionMapping[r.name]])+")", text)

        # Prepend "e." or "s." to propositions for JTLV
        for i, sensor in enumerate(sensorList):
            text = re.sub("\\b"+sensor+"\\b", "e." + sensor, text)
            sensorList[i] = "e." + sensorList[i]

        for i, prop in enumerate(robotPropList):
            text = re.sub("\\b"+prop+"\\b", "s." + prop, text)
            robotPropList[i] = "s." + robotPropList[i]

        regionList = [x.name for x in self.parser.proj.rfi.regions]

        # Define the number of bits needed to encode the regions
        numBits = int(math.ceil(math.log(len(regionList),2)))

        # creating the region bit encoding
        bitEncode = bitEncoding(len(regionList),numBits)
        currBitEnc = bitEncode['current']
        nextBitEnc = bitEncode['next']

        # switch to bit encodings for regions
        if self.proj.compile_options["use_region_bit_encoding"]:
            text = replaceRegionName(text, bitEncode, regionList)

        text = self.substituteMacros(text)

        return text
    
    def splitSpecIntoComponents(self, env, sys):
        spec = {}

        for agent, text in (("env", env), ("sys", sys)):
            for line in text.split("\n"):
                if line.strip() == '': continue

                if "[]<>" in line: 
                    linetype = "goals"
                elif "[]" in line:
                    linetype = "trans"
                else:
                    linetype = "init"

                key = agent.title()+linetype.title()
                if key not in spec:
                    spec[key] = ""

                spec[key] += line + "\n"

        return spec
        
    def _checkForEmptyGaits(self):
        from simulator.ode.ckbot import CKBotLib

        # Initialize gait library
        self.library = CKBotLib.CKBotLib()

        err = 0
        libs = self.library
        libs.readLibe()
		# Check that each individual trait has a corresponding config-gait pair
        robotPropList = self.proj.enabled_actuators + self.proj.all_customs
        for act in robotPropList:
            act = act.strip("u's.")
            if act[0] == "T":
                act = act.strip("T_")
                #print act
                words = act.split("_and_")
                #print words
                config = libs.findGait(words)
                #print config
                if type(config) == type(None):
                    err_message = "WARNING: No config-gait pair for actuator T_" + act + "\n"
                    print err_message
                    err = 1

    def _getGROneCommand(self, module):
        # Check that GROneMain, etc. is compiled
        if not os.path.exists(os.path.join(self.proj.ltlmop_root,"etc","jtlv","GROne","GROneMain.class")):
            print "Please compile the synthesis Java code first.  For instructions, see etc/jtlv/JTLV_INSTRUCTIONS."
            # TODO: automatically compile for the user
            return None

        # Windows uses a different delimiter for the java classpath
        if os.name == "nt":
            delim = ";"
        else:
            delim = ":"

        classpath = delim.join([os.path.join(self.proj.ltlmop_root, "etc", "jtlv", "jtlv-prompt1.4.0.jar"), os.path.join(self.proj.ltlmop_root, "etc", "jtlv", "GROne")])

        cmd = ["java", "-ea", "-Xmx512m", "-cp", classpath, module, self.proj.getFilenamePrefix() + ".smv", self.proj.getFilenamePrefix() + ".ltl"]

        return cmd

    def _autIsNonTrivial(self):
        """
        Check for a) empty automaton, or b) trivial initial-state automaton
         with no transitions
        (This can indicate unsatisfiable system initial conditions (case a),
         or an unsat environment (case b).)

        TODO: Do this in the Java code; it's super inefficient to
        load the whole aut just to check this.
        """

        proj_copy = deepcopy(self.proj)
        proj_copy.rfi = self.parser.proj.rfi
        proj_copy.sensor_handler = None
        proj_copy.actuator_handler = None
        proj_copy.h_instance = None

        aut = fsa.Automaton(proj_copy)

        aut.loadFile(self.proj.getFilenamePrefix()+".aut", self.proj.enabled_sensors, self.proj.enabled_actuators, self.proj.all_customs)        
        
        nonTrivial = any([len(s.transitions) > 0 for s in aut.states])

        return nonTrivial

    def _analyze(self):
        cmd = self._getGROneCommand("GROneDebug")
        if cmd is None:
            return (False, False, [], "")

        subp = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=False)

        realizable = False    
        unsat = False
        nonTrivial = False
        

        output = ""
        to_highlight = []
        for dline in subp.stdout:
            output += dline

            if "Specification is realizable" in dline:   
                realizable = True            
                nonTrivial = self._autIsNonTrivial()
                if nonTrivial:
                    break
            
            ### Highlight sentences corresponding to identified errors ###

            # System unsatisfiability
            elif "System initial condition is unsatisfiable." in dline:
                to_highlight.append(("sys", "init"))
            elif "System transition relation is unsatisfiable." in dline:
                to_highlight.append(("sys", "trans"))
            elif "System highlighted goal(s) unsatisfiable" in dline:
                for l in (dline.strip()).split()[-1:]:
                    to_highlight.append(("sys", "goals", int(l)))
            elif "System highlighted goal(s) inconsistent with transition relation" in dline:
                to_highlight.append(("sys", "trans"))
                to_highlight.append(("sys", "init"))
                for l in (dline.strip()).split()[-1:]:
                    to_highlight.append(("sys", "goals", int(l)))
            elif "System initial condition inconsistent with transition relation" in dline:
                to_highlight.append(("sys", "init"))
                to_highlight.append(("sys", "trans"))
           
            # Environment unsatisfiability
            elif "Environment initial condition is unsatisfiable." in dline:
                to_highlight.append(("env", "init"))
            elif "Environment transition relation is unsatisfiable." in dline:
                to_highlight.append(("env", "trans"))
            elif "Environment highlighted goal(s) unsatisfiable" in dline:
                for l in (dline.strip()).split()[-1:]:
                    to_highlight.append(("env", "goals", int(l)))
            elif "Environment highlighted goal(s) inconsistent with transition relation" in dline:
                to_highlight.append(("env", "init"))
                to_highlight.append(("env", "trans"))
                for l in (dline.strip()).split()[-1:]:
                    to_highlight.append(("env", "goals", int(l)))
            elif "Environment initial condition inconsistent with transition relation" in dline:
                to_highlight.append(("env", "init"))
                to_highlight.append(("env", "trans"))
           
        
            # System unrealizability
            elif "System is unrealizable because the environment can force a safety violation" in dline:
                to_highlight.append(("sys", "trans"))
                to_highlight.append(("sys", "init"))
            elif "System highlighted goal(s) unrealizable" in dline:
                to_highlight.append(("sys", "trans"))
                to_highlight.append(("sys", "init"))
                for l in (dline.strip()).split()[-1:]:
                    to_highlight.append(("sys", "goals", int(l)))
            
            # Environment unrealizability
            elif "Environment is unrealizable because the system can force a safety violation" in dline:
                to_highlight.append(("env", "trans"))
            elif "Environment highlighted goal(s) unrealizable" in dline:
                to_highlight.append(("env", "trans"))
                for l in (dline.strip()).split()[-1:]:
                    to_highlight.append(("env", "goals", int(l)))
                    
            if "unsatisfiable" in dline or "inconsistent" in dline :
                unsat = True

        subp.stdout.close()
        
        
        
        return (realizable, unsat, nonTrivial, to_highlight, output)
    
    
    def _coreFinding(self, to_highlight, unsat, badInit):
        #find number of states in automaton/counter for unsat/unreal core max unrolling depth ("recurrence diameter")
        proj_copy = deepcopy(self.proj)
        proj_copy.rfi = self.parser.proj.rfi
        proj_copy.sensor_handler = None
        proj_copy.actuator_handler = None
        proj_copy.h_instance = None
    
        aut = fsa.Automaton(proj_copy)
        aut.loadFile(self.proj.getFilenamePrefix()+".aut", self.proj.enabled_sensors, self.proj.enabled_actuators, self.proj.all_customs)        
        numStates = len(aut.states)
        
        numRegions = len(self.parser.proj.rfi.regions)
#        robotPropList = self.proj.enabled_actuators + self.proj.all_customs
#        regList = map(lambda i: "bit"+str(i), range(0,int(numpy.ceil(numpy.log2(len(regionList))))))
        
#        numStates = 2**len(regList + robotPropList)
        
        
        #get conjuncts to be minimized
        topo, conjuncts = self.getGuiltyConjuncts(to_highlight, badInit)
        
        if unsat:
            guilty = self.findCoresUnsat(topo,badInit,conjuncts,15,15)#returns LTL  
        else:
            guilty = self.findCoresUnreal(topo,badInit,conjuncts,numStates,numRegions)#returns LTL   
        return guilty
        
        
        
    
    def findCoresUnsat(self,topo, badInit, conjuncts,maxDepth,numRegions):
        depth = numRegions
        cmd = self._getPicosatCommand() 
        numProps = len(self.propList)
        if not conjuncts and badInit == "":
            return []
        #first try without topo and init, see if it is satisfiable
        mapping, cnfMapping, init, self.trans, goals = conjunctsToCNF([badInit], conjuncts, self.propList)
            
           
        pool = Pool()
                      
            #print "STARTING PICO MAP"
            
        guiltyList = pool.map(findGuiltyClausesWrapper, itertools.izip(itertools.repeat(cmd),range(0,maxDepth + 1), itertools.repeat(numProps), itertools.repeat(init), itertools.repeat(self.trans), itertools.repeat(goals), itertools.repeat(mapping), itertools.repeat(cnfMapping), itertools.repeat(conjuncts)))
            #allGuilty = map((lambda (depth, cnfs): self.guiltyParallel(depth+1, cnfs, mapping)), list(enumerate(allCnfs)))
            #print "ENDING PICO MAP"
            
        pool.terminate()
            
        allGuilty = set([item for g,idx in [x for x in guiltyList if x] for item in g])
        nonGoalIndicesOld = [item for g,idx in [x for x in guiltyList if x] for item in idx if '<>' not in g]       
        
            
        if all(guiltyList):
            print "unsat core found without topo and init"
            return allGuilty
        else:
            depth += len([g for g in allGuilty if g])
        
        #then try just topo and init and see if it is unsatisfiable. If so, return core.
        mapping,  cnfMapping, init, self.trans, goals = conjunctsToCNF([topo, badInit], [], self.propList)
       
                    
        guiltyAndInds = findGuiltyClauses(cmd,maxDepth,numProps,init,self.trans,goals,mapping,cnfMapping,[topo, badInit])
        
                #allGuilty = map((lambda (depth, cnfs): self.guiltyParallel(depth+1, cnfs, mapping)), list(enumerate(allCnfs)))
            #print "ENDING PICO MAP"
            
            
            
        if guiltyAndInds:
            print "unsat core found with just topo and init"
            return guiltyAndInds[1]
        
        #if the problem is in conjunction with the topo but not just topo, keep increasing the depth until something more than just topo is returned
        mapping,  cnfMapping, init, self.trans, goals = conjunctsToCNF([topo,badInit], conjuncts, self.propList)
        
        pool = Pool()
                      
            #print "STARTING PICO MAP"
            
        guiltyList = pool.map(findGuiltyClausesWrapper, itertools.izip(itertools.repeat(cmd),range(0,maxDepth + 1), itertools.repeat(numProps), itertools.repeat(init), itertools.repeat(self.trans), itertools.repeat(goals), itertools.repeat(mapping),  itertools.repeat(cnfMapping), itertools.repeat([topo, badInit]+conjuncts)))
            #allGuilty = map((lambda (depth, cnfs): self.guiltyParallel(depth+1, cnfs, mapping)), list(enumerate(allCnfs)))
            #print "ENDING PICO MAP"
            
        pool.terminate()
        
        guilty = [item for g,idx in guiltyList for item in g] 
        cnfIndices = [item for g,idx in guiltyList for item in idx]       
                
        
        
        guiltyMinusGoal = [g for g in guilty if '<>' not in g]
                        
        justTopo = set([topo, badInit]).issuperset(guiltyMinusGoal)
        depth = maxDepth + 1
        
        while justTopo:
            
            guilty, cnfIndices = findGuiltyClauses(cmd,depth,numProps,init,self.trans,goals,mapping,cnfMapping,[topo, badInit]+conjuncts)
            #allGuilty = map((lambda (depth, cnfs): self.guiltyParallel(depth+1, cnfs, mapping)), list(enumerate(allCnfs)))
            #print "ENDING PICO MAP"
            
            
            guiltyMinusGoal = [g for g in guilty if '<>' not in g]
            if not set([topo, badInit]).issuperset(set(guiltyMinusGoal)):
                justTopo = False
            else:
                depth+=1
            #get contributing conjuncts from CNF indices            
            #guilty = cnfToConjuncts(allIndices, mapping)
        
        print "unsat core found with all parts"          
        #remove non-goal clauses that were found in no topo check            
        guilty = cnfToConjuncts((set(cnfIndices) - set(nonGoalIndicesOld)), mapping, cnfMapping)
        print guilty
        
        return guilty
    
          
        
                
    
        
    def findCoresUnreal(self,topo,badInit,conjuncts,maxDepth,numRegions):
        #get conjuncts to be minimized
        return []#self.findCoresUnsat(topo,badInit,conjuncts,maxDepth,numRegions)
        
        
    def _getPicosatCommand(self):
        # look for picosat

        paths = [p for p in glob.glob(os.path.join(self.proj.ltlmop_root,"lib","cores","picosat-*")) if os.path.isdir(p)]
        if len(paths) == 0:
            print "Where is your sat solver? We use Picosat."
            # TODO: automatically compile for the user
            return None
        else:
            print "Found Picosat in " + paths[0]

        if os.name == "nt":
            cmd = os.path.join(paths[0],"picomus.exe")
        else:
            cmd = [os.path.join(paths[0],"picomus")]

        return cmd
    
    def getGuiltyConjuncts(self, to_highlight, badInit):  
        #inverse dictionary for goal lookups
        #ivd=dict([(v,k) for (k,v) in self.LTL2LineNo.items()])
        
        topo=self.spec['Topo'].replace('\n','')
        topo = topo.replace('\t','')
        
        if badInit != "":
            conjuncts = [badInit]
        else:
            conjuncts = []
                
        for h_item in to_highlight:
            tb_key = h_item[0].title() + h_item[1].title()

            newCs = []
            if h_item[1] == "goals":
                #special treatment for goals: (1) we already know which one to highlight, and (2) we need to check both tenses
                #TODO: separate out the check for present and future tense -- what if you have to toggle but can still do so infinitely often?
                #newCs = ivd[self.traceback[tb_key][h_item[2]]].split('\n')                 
                goals = ["[]<>(TRUE)"] + self.spec[tb_key].split('\n')
                newCs = [goals[h_item[2]]]
                newCsOld = newCs
                """for p in self.propList:
                    old = ''+str(p)
                    new = 'next('+str(p)+')'
                    newCs = map(lambda s: s.replace(old,new), newCs) 
                """                           
                #newCs.extend(newCsOld)
            elif h_item[1] == "trans" or not badInit:
                #newCs = self.spec[tb_key].split('\n')
                newCs =  self.spec[tb_key].replace("\t", "\n").split("\n")
                #newCs = [k.split('\n') for k,v in self.LTL2LineNo.iteritems() if v in self.traceback[tb_key]]
                #newCs = [item for sublist in newCs for item in sublist]                
            """for clause in newCs:
                #need to mark trans lines because they do not always contain [] because of line breaks
                if h_item[1] == "trans":
                    isTrans[clause] = 1                    
                else:
                    isTrans[clause] = 0  
            """
            conjuncts.extend(newCs)
        
        #filter out props that are actually used
        self.propList = [p for p in self.propList if [c for c in conjuncts if p in c] or p in topo]
            
            
            
    
        return topo, conjuncts

    def _synthesize(self, with_safety_aut=False):
        cmd = self._getGROneCommand("GROneMain")
        if cmd is None:
            return (False, False, "")

        if with_safety_aut:    # Generally used for Mopsy
            cmd.append("--safety")

        if self.proj.compile_options["fastslow"]:
            cmd.append("--fastslow")

        subp = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=False)
        
        realizable = False
        realizableFS = False

        output = ""
        for line in subp.stdout:
            output += line
            if "Specification is realizable" in line:
                realizable = True
            if "Specification is realizable with slow and fast actions" in line:
                realizableFS = True
               
        subp.stdout.close()

        return (realizable, realizableFS, output)

    def compile(self, with_safety_aut=False):
        if self.proj.compile_options["decompose"]:
            print "--> Decomposing..."
            self._decompose()
        print "--> Writing LTL file..."
        spec, tb, resp = self._writeLTLFile()
        print "--> Writing SMV file..."
        self._writeSMVFile()

        if tb is None:
            print "ERROR: Compilation aborted"
            return 

        #self._checkForEmptyGaits()
        print "--> Synthesizing..."
        return self._synthesize(with_safety_aut)

