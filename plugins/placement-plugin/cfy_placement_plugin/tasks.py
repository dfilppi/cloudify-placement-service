########
# Copyright (c) 2019 Cloudify Platform All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#    * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    * See the License for the specific language governing permissions and
#    * limitations under the License.

from cloudify import ctx
from cloudify_rest_client import CloudifyClient
from cloudify_rest_client.executions import Execution
from cloudify import manager
from flask import Flask, abort, request, jsonify
from flask_selfdoc import Autodoc
from threading import Thread
from datetime import datetime
import random
import json
import time
import os

def start(**kwargs):
    log("Starting placement service")
    ''' Starts the placement service
    '''

    # For DEBUGGING locally
    if ctx._local:
        client = CloudifyClient(
            host = '10.239.2.83',
            username = 'admin',
            password = 'admin',
            tenant = 'default_tenant')
    else:
        client = manager.get_rest_client()

    r,w = os.pipe()
    pid = os.fork()
    if pid > 0:
        # wait for pid on pipe
        os.close(w)
        for i in range(10):
            pid = os.read(r, 10)
            if pid == "":
                time.sleep(1)
                log("waiting for pid")
                continue
            else:
                ctx.instance.runtime_properties["pid"] = str(pid)
                break
        if pid == "":
            log("ERROR: Failed to get child PID")
        os.close(r)
        return

    os.close(r)
    os.chdir("/tmp")
    os.setsid()
    os.umask(0)
    close_fds([w])

    pid = os.fork()
    if pid > 0:
        log("INFO: child pid = "+str(pid))
        os.write(w,str(pid))
        os.close(w)
        os._exit(0)
    os.close(w)

    # Needed by Flask
    os.open("/dev/null", os.O_RDONLY)
    os.open("/dev/null", os.O_WRONLY)

    # Start REST server
    app = Flask(__name__)
    auto = Autodoc(app)

    # init stats
    stats = {}
    stats['errcnt'] = 0
    stats['actions'] = []

    # init config
    config = {}
    config['log_location'] = '/tmp/log'

    try:
        set_routes(app, auto, ctx.node.properties, stats, config, client)
        rest = Thread(target=app.run, kwargs={"host":"0.0.0.0", "port": ctx.node.properties['port'], "debug":False})
        rest.start()
    except Exception as e:
        log(str(e))
        os._exit(0)

    rest.join()

    os._exit(0)


def stop(**kwargs):
    ''' Stops the operator process
    '''
    pid = ctx.instance.runtime_properties['pid']

    ctx.logger.info("stopping process {}".format(pid))

    res = os.system("kill "+str(pid))
    if res != 0:
        ctx.logger.error("kill failed for pid ".format(pid))


def log(message):
    with open("/tmp/log", "a+") as f:
        f.write(datetime.now().strftime("%y%m%dT%H%M%S")+" "+message+"\n")
        f.flush()


def close_fds(leave_open=[0, 1, 2]):
    fds = os.listdir(b'/proc/self/fd')
    for fdn in fds:
        fd = int(fdn)
        if fd not in leave_open:
            try:
                os.close(fd)
            except Exception:
                pass


def get_bid(criteria):
    ''' Return a blueprint id and args based on criteria '''
    log("INFO: criteria = " + str(criteria))
    return "b1", None

# TODO: should check for collisions
def gen_did( base ):
    ''' Generate a deployment id '''
    return base + "_" + hex(random.randint(0,2**32)).upper()[2:]

#############################
## REST API
#############################

FAILED_STATES = ( Execution.CANCELLED,
                  Execution.CANCELLING,
                  Execution.FORCE_CANCELLING,
                  Execution.FAILED )

def set_routes(app, auto, properties, stats, config, client):
    @app.errorhandler(400)
    def custom400(error):
        response = jsonify({'message': error.description})
        response.status_code = 404
        response.status = 'error.Bad Request'
        return response

    @app.errorhandler(500)
    def custom500(error):
        response = jsonify({'message': error.description})
        response.status_code = 404
        response.status = 'error.Bad Request'
        return response

    @app.route('/')
    def hello_world():
        return auto.html()

    @auto.doc()
    @app.route('/deployments', methods=['POST'])
    def do_install():
        try:
            body = request.json
            if not body:
                abort(400, 'no POST body')
            log("DEBUG: got body = "+str(body))
    
            bid, binputs = get_bid(body) 
            if not bid:
                # no blueprint id returned by rules
                abort(400, 'blueprint can be resolved by rules')
    
            blueprint = client.blueprints.get(bid)
            if not blueprint:
                abort(400, "blueprint " + bid + "not found")
    
            did = gen_did(bid)
            deployment = client.deployments.create( bid, did, binputs)
            log("DEBUG: deployment created:"+did)
    
            done = False
            for _ in range(30):
                log("INFO: waiting for deployment ex")
                executions = client.executions.list(deployment_id = deployment.id)
                log("DEBUG: # executions: " + str(len(executions)))
                if len(executions) == 1:
                    log("DEBUG: exe status = "+executions[0].status)
                    if executions[0].status == executions[0].TERMINATED:
                        done = True
                        break
                    elif executions[0].status in FAILED_STATES:
                        abort( 500, "deployment execution failed" )
                time.sleep(1)
            if not done:
                abort(500, "deployment init timed out for id = " + deployment.id)
            
            log("INFO: starting install")
            execution = client.executions.start(
                            deployment.id,
                            "install")
        except Exception as e:
            log("ERR: exception + " + e.message)
            abort(500, "exception caught:"+e.message)
            
        return ("{ \"execution_id\": \"" + execution.id +
                   ",\"deployment_id\": \"" + did +
                 "\"}")
                 
