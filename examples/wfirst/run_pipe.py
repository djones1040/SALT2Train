import sys
sys.path.append('/project2/rkessler/SURVEYS/WFIRST/ROOT/SALT3/salt3')
import pandas as pd
from importlib import reload
import pipeline.pipeline
#reload(pipeline.pipeline)
from pipeline.pipeline import *

def run_wfirst_pipeline():
    pipe = SALT3pipe(finput='run_wfirst.txt')
    pipe.build(data=False,mode='customize',onlyrun=['sim','lcfit','getmu'])
    pipe.configure()
    #pipe.glue(['byosed','sim'])
    pipe.glue(['sim','lcfit'],on='phot')
    #pipe.glue(['train','lcfit'],on='model')
    pipe.glue(['lcfit','getmu'])
    pipe.run(onlyrun=['sim','lcfit','getmu'])
    #pipe.glue(['getmu','cosmofit'])
    #pipe.run(onlyrun=['getmu'])

if __name__=='__main__':
    run_wfirst_pipeline()
