#!/usr/bin/env python3

################################################################################
# SPDX-FileCopyrightText: Copyright (c) 2019-2021 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
################################################################################

import sys
sys.path.append('../')
import gi
import configparser
gi.require_version('Gst', '1.0')
from gi.repository import GObject, Gst
from gi.repository import GLib
from ctypes import *
import numpy as np
import ctypes
import sys
import math
import platform
from common.is_aarch_64 import is_aarch64
from common.bus_call import bus_call
from common.FPS import GETFPS



import pyds
from utils import load_dataset, normalize_vectors, predict_using_classifier, save_embeddings, save_entry_log

fps_streams={}

MAX_DISPLAY_LEN=64
PRIMARY_DETECTOR_UID = 1
PGIE_CLASS_ID_FACE = 0
MUXER_OUTPUT_WIDTH=1920
MUXER_OUTPUT_HEIGHT=1080
MUXER_BATCH_TIMEOUT_USEC=4000000
TILED_OUTPUT_WIDTH=1280
TILED_OUTPUT_HEIGHT=720
GST_CAPS_FEATURES_NVMM="memory:NVMM"
OSD_PROCESS_MODE= 0
OSD_DISPLAY_TEXT= 1
pgie_classes_str= ["face"]

# DATASET_PATH = 'embeddings/psu_embeddings.npz'

# faces_embeddings, labels = load_dataset(DATASET_PATH)
user_meta = {}

unknown_emb_count = 0
def sgie_sink_pad_buffer_probe(pad,info,u_data):
    
    frame_number=0
    
    num_rects=0
    gst_buffer = info.get_buffer()
    if not gst_buffer:
        print("Unable to get GstBuffer ")
        return

    # Retrieve batch metadata from the gst_buffer
    # Note that pyds.gst_buffer_get_nvds_batch_meta() expects the
    # C address of gst_buffer as input, which is obtained with hash(gst_buffer)
    batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
    l_frame = batch_meta.frame_meta_list
    while l_frame is not None:
        try:
            # Note that l_frame.data needs a cast to pyds.NvDsFrameMeta
            # The casting is done by pyds.NvDsFrameMeta.cast()
            # The casting also keeps ownership of the underlying memory
            # in the C code, so the Python garbage collector will leave
            # it alone.
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
        except StopIteration:
            break

        frame_number=frame_meta.frame_num
        num_rects = frame_meta.num_obj_meta
        print("frame_number",frame_number)
        if frame_meta.pad_index in user_meta:
            user_meta[frame_meta.pad_index] = {}
        for k in user_meta[frame_meta.pad_index]:
            user_meta[frame_meta.pad_index][k] = user_meta[frame_meta.pad_index][k] + 1
            if user_meta[frame_meta.pad_index][k] > 100:
                del user_meta[frame_meta.pad_index][k]
        l_obj=frame_meta.obj_meta_list

        while l_obj is not None:
            try:
                # Casting l_obj.data to pyds.NvDsObjectMeta
                obj_meta=pyds.NvDsObjectMeta.cast(l_obj.data)
            except StopIteration:
                break
            
            # if obj_meta.class_id == SGIE_CLASS_ID_FACE:
            #     print(f'obj_meta.obj_user_meta_list {l_user}')
            if obj_meta.unique_component_id == PRIMARY_DETECTOR_UID:
                if obj_meta.class_id == PGIE_CLASS_ID_FACE:
                    l_user = obj_meta.obj_user_meta_list

                    while l_user is not None:
                    
                        try:
                            # Casting l_user.data to pyds.NvDsUserMeta
                            user_meta=pyds.NvDsUserMeta.cast(l_user.data)
                        except StopIteration:
                            break

                        if (
                            user_meta.base_meta.meta_type
                            != pyds.NvDsMetaType.NVDSINFER_TENSOR_OUTPUT_META
                        ):
                            continue
                        
                        # Converting to tensor metadata
                        # Casting user_meta.user_meta_data to NvDsInferTensorMeta
                        tensor_meta = pyds.NvDsInferTensorMeta.cast(user_meta.user_meta_data)
                        
                        # Get output layer as NvDsInferLayerInfo 
                        layer = pyds.get_nvds_LayerInfo(tensor_meta, 0)

                        # Convert NvDsInferLayerInfo buffer to numpy array
                        ptr = ctypes.cast(pyds.get_ptr(layer.buffer), ctypes.POINTER(ctypes.c_float))
                        v = np.ctypeslib.as_array(ptr, shape=(128,))
                        
                        # Pridict face neme
                        yhat = v.reshape((1,-1))
                        face_to_predict_embedding = normalize_vectors(yhat)
                        print("face_to_predict_embedding",face_to_predict_embedding, obj_meta.confidence, obj_meta.object_id)
                        df = load_dataset()
                        all_indexes = list(df.index.values)
                        result = predict_using_classifier(df.to_numpy(), face_to_predict_embedding)
                        maxi = np.argmax(result)
                        maxval = np.max(result)
                        if maxval > 0.65:
                            if (all_indexes[maxi] in user_meta[frame_meta.pad_index] and user_meta[frame_meta.pad_index][all_indexes[maxi]] > 50) or (all_indexes[maxi] not in user_meta[frame_meta.pad_index]):
                                print("Match Found", all_indexes[maxi], maxval)
                                save_entry_log(all_indexes[maxi], frame_meta.pad_index)
                                user_meta[frame_meta.pad_index][all_indexes[maxi]] = 0
                        elif maxval < 0.55:
                            print("Unknown found")
                            save_embeddings(face_to_predict_embedding, "unk_"+str(unknown_emb_count))
                            unknown_emb_count = unknown_emb_count +1
                            user_meta[frame_meta.pad_index]["unk_"+str(unknown_emb_count)] = 0
                            ## add embeding
                        # result =  (str(result).title())
                        # print('Predicted name: %s' % result)
                        
                        # Generate classifer metadata and attach to obj_meta
                        
                        # Get NvDsClassifierMeta object 
                        classifier_meta = pyds.nvds_acquire_classifier_meta_from_pool(batch_meta)

                        # Pobulate classifier_meta data with pridction result
                        classifier_meta.unique_component_id = tensor_meta.unique_id
                        
                        
                        label_info = pyds.nvds_acquire_label_info_meta_from_pool(batch_meta)

                        
                        label_info.result_prob = 0
                        label_info.result_class_id = 0

                        pyds.nvds_add_label_info_meta_to_classifier(classifier_meta, label_info)
                        pyds.nvds_add_classifier_meta_to_object(obj_meta, classifier_meta)

                        display_text = pyds.get_string(obj_meta.text_params.display_text)
                        obj_meta.text_params.display_text = f'{display_text} {""}'

                        try:
                            l_user = l_user.next
                        except StopIteration:
                            break

            try: 
                l_obj=l_obj.next
            except StopIteration:
                break
        try:
            l_frame=l_frame.next
        except StopIteration:
            break
    return Gst.PadProbeReturn.OK



def cb_newpad(decodebin, decoder_src_pad,data):
    print("In cb_newpad\n")
    caps=decoder_src_pad.get_current_caps()
    gststruct=caps.get_structure(0)
    gstname=gststruct.get_name()
    source_bin=data
    features=caps.get_features(0)

    # Need to check if the pad created by the decodebin is for video and not
    # audio.
    print("gstname=",gstname)
    if(gstname.find("video")!=-1):
        # Link the decodebin pad only if decodebin has picked nvidia
        # decoder plugin nvdec_*. We do this by checking if the pad caps contain
        # NVMM memory features.
        print("features=",features)
        if features.contains("memory:NVMM"):
            # Get the source bin ghost pad
            bin_ghost_pad=source_bin.get_static_pad("src")
            if not bin_ghost_pad.set_target(decoder_src_pad):
                sys.stderr.write("Failed to link decoder src pad to source bin ghost pad\n")
        else:
            sys.stderr.write(" Error: Decodebin did not pick nvidia decoder plugin.\n")

def decodebin_child_added(child_proxy,Object,name,user_data):
    print("Decodebin child added:", name, "\n")
    if(name.find("decodebin") != -1):
        Object.connect("child-added",decodebin_child_added,user_data)

    # if "source" in name:
    #     Object.set_property("drop-on-latency", True)


def create_source_bin(index,uri):
    print("Creating source bin")

    # Create a source GstBin to abstract this bin's content from the rest of the
    # pipeline
    bin_name="source-bin-%02d" %index
    print(bin_name)
    nbin=Gst.Bin.new(bin_name)
    if not nbin:
        sys.stderr.write(" Unable to create source bin \n")

    # Source element for reading from the uri.
    # We will use decodebin and let it figure out the container format of the
    # stream and the codec and plug the appropriate demux and decode plugins.
    uri_decode_bin=Gst.ElementFactory.make("uridecodebin", "uri-decode-bin")
    if not uri_decode_bin:
        sys.stderr.write(" Unable to create uri decode bin \n")
    # We set the input uri to the source element
    uri_decode_bin.set_property("uri",uri)
    # Connect to the "pad-added" signal of the decodebin which generates a
    # callback once a new pad for raw data has beed created by the decodebin
    uri_decode_bin.connect("pad-added",cb_newpad,nbin)
    uri_decode_bin.connect("child-added",decodebin_child_added,nbin)

    # We need to create a ghost pad for the source bin which will act as a proxy
    # for the video decoder src pad. The ghost pad will not have a target right
    # now. Once the decode bin creates the video decoder and generates the
    # cb_newpad callback, we will set the ghost pad target to the video decoder
    # src pad.
    Gst.Bin.add(nbin,uri_decode_bin)
    bin_pad=nbin.add_pad(Gst.GhostPad.new_no_target("src",Gst.PadDirection.SRC))
    if not bin_pad:
        sys.stderr.write(" Failed to add ghost pad in source bin \n")
        return None
    return nbin

def main(args):
    # Check input arguments
    if len(args) < 2:
        sys.stderr.write("usage: %s <uri1> [uri2] ... [uriN]\n" % args[0])
        sys.exit(1)

    for i in range(0,len(args)-1):
        fps_streams["stream{0}".format(i)]=GETFPS(i)
    number_sources=len(args)-1

    # Standard GStreamer initialization
    GObject.threads_init()
    Gst.init(None)

    # Create gstreamer elements */
    # Create Pipeline element that will form a connection of other elements
    print("Creating Pipeline \n ")
    pipeline = Gst.Pipeline()
    is_live = False

    if not pipeline:
        sys.stderr.write(" Unable to create Pipeline \n")
    print("Creating streamux \n ")

    # Create nvstreammux instance to form batches from one or more sources.
    streammux = Gst.ElementFactory.make("nvstreammux", "Stream-muxer")
    if not streammux:
        sys.stderr.write(" Unable to create NvStreamMux \n")

    pipeline.add(streammux)
    for i in range(number_sources):
        print("Creating source_bin ",i," \n ")
        uri_name=args[i+1]
        if uri_name.find("rtsp://") == 0 :
            is_live = True
        source_bin=create_source_bin(i, uri_name)
        if not source_bin:
            sys.stderr.write("Unable to create source bin \n")
        pipeline.add(source_bin)
        padname="sink_%u" %i
        sinkpad= streammux.get_request_pad(padname)
        if not sinkpad:
            sys.stderr.write("Unable to create sink pad bin \n")
        srcpad=source_bin.get_static_pad("src")
        if not srcpad:
            sys.stderr.write("Unable to create src pad bin \n")
        srcpad.link(sinkpad)
    queue1=Gst.ElementFactory.make("queue","queue1")
    queue2=Gst.ElementFactory.make("queue","queue2")
    queue3=Gst.ElementFactory.make("queue","queue3")
    queue4=Gst.ElementFactory.make("queue","queue4")
    queue5=Gst.ElementFactory.make("queue","queue5")
    pipeline.add(queue1)
    pipeline.add(queue2)
    pipeline.add(queue3)
    pipeline.add(queue4)
    pipeline.add(queue5)

    print("Creating tracker \n ")
    tracker = Gst.ElementFactory.make("nvtracker", "tracker")
    if not tracker:
        sys.stderr.write(" Unable to create tracker \n")

    print("Creating Pgie \n ")
    pgie = Gst.ElementFactory.make("nvinfer", "primary-inference")
    if not pgie:
        sys.stderr.write(" Unable to create pgie \n")
    
    print("Creating Sgie \n ")
    sgie = Gst.ElementFactory.make("nvinfer", "secondary-inference")
    if not pgie:
        sys.stderr.write(" Unable to create sgie \n")

    print("Creating tiler \n ")
    tiler=Gst.ElementFactory.make("nvmultistreamtiler", "nvtiler")
    if not tiler:
        sys.stderr.write(" Unable to create tiler \n")
    print("Creating nvvidconv \n ")
    nvvidconv = Gst.ElementFactory.make("nvvideoconvert", "convertor")
    if not nvvidconv:
        sys.stderr.write(" Unable to create nvvidconv \n")
    print("Creating nvosd \n ")

    fakesink = Gst.ElementFactory.make("fakesink", "fakesink")
    # nvosd = Gst.ElementFactory.make("nvdsosd", "onscreendisplay")
    # if not nvosd:
    #     sys.stderr.write(" Unable to create nvosd \n")
    # nvosd.set_property('process-mode',OSD_PROCESS_MODE)
    # nvosd.set_property('display-text',OSD_DISPLAY_TEXT)

    # if(is_aarch64()):
    #     print("Creating transform \n ")
    #     transform=Gst.ElementFactory.make("nvegltransform", "nvegl-transform")
    #     if not transform:
    #         sys.stderr.write(" Unable to create transform \n")

    # print("Creating EGLSink \n")
    # sink = Gst.ElementFactory.make("nveglglessink", "nvvideo-renderer")
    # if not sink:
    #     sys.stderr.write(" Unable to create egl sink \n")

    if is_live:
        print("Atleast one of the sources is live")
        streammux.set_property('live-source', 1)

    streammux.set_property('width', 1920)
    streammux.set_property('height', 1080)
    streammux.set_property('batch-size', number_sources)
    streammux.set_property('batched-push-timeout', 4000000)
    pgie.set_property('config-file-path', "face_detector_config.txt")
    pgie_batch_size=pgie.get_property("batch-size")
    if(pgie_batch_size != number_sources):
        print("WARNING: Overriding infer-config batch-size",pgie_batch_size," with number of sources ", number_sources," \n")
        pgie.set_property("batch-size",number_sources)

    sgie.set_property('config-file-path', "facenet_config.txt")
    sgie_batch_size=sgie.get_property("batch-size")
    if(sgie_batch_size != number_sources):
        print("WARNING: Overriding infer-config batch-size",sgie_batch_size," with number of sources ", number_sources," \n")
        sgie.set_property("batch-size",number_sources)

    tiler_rows=int(math.sqrt(number_sources))
    tiler_columns=int(math.ceil((1.0*number_sources)/tiler_rows))
    tiler.set_property("rows",tiler_rows)
    tiler.set_property("columns",tiler_columns)
    tiler.set_property("width", TILED_OUTPUT_WIDTH)
    tiler.set_property("height", TILED_OUTPUT_HEIGHT)
    # sink.set_property("qos",0)

    config = configparser.ConfigParser()
    config.read('tracker_config.txt')
    config.sections()

    for key in config['tracker']:
        if key == 'tracker-width' :
            tracker_width = config.getint('tracker', key)
            tracker.set_property('tracker-width', tracker_width)
        if key == 'tracker-height' :
            tracker_height = config.getint('tracker', key)
            tracker.set_property('tracker-height', tracker_height)
        if key == 'gpu-id' :
            tracker_gpu_id = config.getint('tracker', key)
            tracker.set_property('gpu_id', tracker_gpu_id)
        if key == 'll-lib-file' :
            tracker_ll_lib_file = config.get('tracker', key)
            tracker.set_property('ll-lib-file', tracker_ll_lib_file)
        if key == 'll-config-file' :
            tracker_ll_config_file = config.get('tracker', key)
            tracker.set_property('ll-config-file', tracker_ll_config_file)
        if key == 'enable-batch-process' :
            tracker_enable_batch_process = config.getint('tracker', key)
            tracker.set_property('enable_batch_process', tracker_enable_batch_process)

    print("Adding elements to Pipeline \n")
    pipeline.add(tracker)
    pipeline.add(pgie)
    pipeline.add(sgie)
    pipeline.add(tiler)
    pipeline.add(nvvidconv)
    pipeline.add(fakesink)
    # if is_aarch64():
    #     pipeline.add(transform)
    # pipeline.add(sink)

    print("Linking elements in the Pipeline \n")
    streammux.link(queue1)
    queue1.link(pgie)
    pgie.link(tracker)
    tracker.link(sgie)
    sgie.link(queue2)
    queue2.link(tiler)
    tiler.link(queue3)
    queue3.link(nvvidconv)
    nvvidconv.link(queue4)
    queue4.link(fakesink)
    # if is_aarch64():
    #     nvosd.link(queue5)
    #     queue5.link(transform)
    #     transform.link(sink)
    # else:
    #     nvosd.link(queue5)
    #     queue5.link(sink)

    # create an event loop and feed gstreamer bus mesages to it
    loop = GObject.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect ("message", bus_call, loop)
    tiler_src_pad=sgie.get_static_pad("src")
    if not tiler_src_pad:
        sys.stderr.write(" Unable to get src pad \n")
    else:
        tiler_src_pad.add_probe(Gst.PadProbeType.BUFFER, sgie_sink_pad_buffer_probe, 0)

    # List the sources
    print("Now playing...")
    for i, source in enumerate(args):
        if (i != 0):
            print(i, ": ", source)

    print("Starting pipeline \n")
    # start play back and listed to events
    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    except:
        pass
    # cleanup
    print("Exiting app\n")
    pipeline.set_state(Gst.State.NULL)

if __name__ == '__main__':
    sys.exit(main(sys.argv))