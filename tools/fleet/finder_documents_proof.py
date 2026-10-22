"""Prepare or run one guarded Documents-sidebar experiment; never retry input.

The model selects only genuine visible sidebar OCR indexes. A successful
experiment still needs independent operator visual review before task_verified.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import time

import brain
from ocr_budget import admit, OCRAdmissionError
import private_ocr_server as private

TASK = 'Click Documents in the Finder sidebar.'
MODEL = 'qwen2.5-coder:0.5b'
OTHER_TITLES = {'Recents', 'Desktop', 'Downloads', 'Applications', 'Home',
                'AirDrop', 'Computer', 'Macintosh HD'}
SIDEBAR_LABELS = OTHER_TITLES | {'Documents', 'Network', 'Pictures', 'Movies', 'Music'}


class ProofRefusal(ValueError):
    pass


def valid_rows(entries):
    if not isinstance(entries, list):
        raise ProofRefusal('bounded OCR entries required')
    rows = []
    for entry in entries[:256]:
        if not isinstance(entry, dict) or not isinstance(entry.get('text'), str):
            continue
        values = [entry.get(key) for key in ('x', 'y', 'width', 'height')]
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
            continue
        x,y,width,height = values
        if not (0 <= x < 1 and 0 <= y < 1 and 0 < width <= 1-x and 0 < height <= 1-y):
            continue
        row = dict(entry)
        row['text'] = entry['text'].strip()
        rows.append(row)
    return rows


def finder_foreground(rows):
    menu = sorted((row for row in rows if .025 <= row['x'] <= .25 and row['y'] >= .94),
                  key=lambda row:row['x'])
    return bool(menu) and menu[0]['text']=='Finder'


def title_candidates(rows, sidebar):
    # The title must lie above and to the right of the recognized sidebar;
    # these are evidence regions, never coordinates invented for an action.
    return [row for row in rows if sidebar['x']+max(.08,sidebar['width']) < row['x'] < .95
            and max(.40,sidebar['y']+.05) < row['y'] < .92]


def before_evidence(entries):
    rows = valid_rows(entries)
    if not finder_foreground(rows):
        raise ProofRefusal('foreground Finder must be independently prepared first')
    candidates = []
    for row in rows:
        if row['text']!='Documents' or not (.01 <= row['x'] <= .5 and .12 <= row['y'] <= .85):
            continue
        neighbors = [other for other in rows if other['text'] in SIDEBAR_LABELS
                     and other['text']!='Documents' and abs(other['x']-row['x']) <= .06
                     and .10 <= other['y'] <= .90]
        if len({other['text'] for other in neighbors}) >= 2:
            candidates.append((row,neighbors))
    if len(candidates)!=1:
        raise ProofRefusal('one Documents sidebar target with two neighboring labels is required')
    sidebar,neighbors = candidates[0]
    titles = title_candidates(rows,sidebar)
    if any(row['text']=='Documents' for row in titles):
        raise ProofRefusal('Documents is already open; no action is warranted')
    title = [row for row in titles if row['text'] in OTHER_TITLES]
    if len(title)!=1:
        raise ProofRefusal('a recognized non-Documents Finder title is required')
    targets = sorted([sidebar]+neighbors, key=lambda row:(-row['y'],row['x']))
    if len(targets)>16:
        raise ProofRefusal('ambiguous sidebar target list')
    return dict(sidebar=sidebar,title=title[0],targets=targets)


def after_evidence(entries,before):
    rows = valid_rows(entries)
    if not finder_foreground(rows):
        return False
    # Match the independently recognized pre-action title band. Merely seeing
    # Documents in the unchanged sidebar cannot prove navigation.
    title = before['title']
    return any(row['text']=='Documents' and abs(row['y']-title['y']) <= .04
               and abs(row['x']-title['x']) <= .3
               for row in title_candidates(rows,before['sidebar']))


class SidebarOnlyVM:
    def __init__(self, vm, trace):
        self.vm,self.trace,self.name = vm,trace,vm.name
        self.baseline = None
        self.frame_before = None
        self.attempted = False

    def health(self): return self.vm.health()
    def frame(self): return self.vm.frame()

    def ocr(self,frame,*,accurate=False):
        evidence = before_evidence(self.vm.ocr(frame,accurate=True))
        self.baseline,self.frame_before = evidence,frame
        with private.private_output(self.trace/'before.jpg') as output:
            output.write(frame.jpeg)
        return evidence['targets']

    def click(self,x,y,*,frame):
        if self.attempted or self.baseline is None or self.frame_before.sha256!=frame.sha256:
            raise ValueError('one fresh Documents click only')
        target = self.baseline['sidebar']
        expected = (round((target['x']+target['width']/2)*(frame.width-1)),
                    round((1-target['y']-target['height']/2)*(frame.height-1)))
        if (x,y)!=expected:
            raise ValueError('the model did not select the visible Documents sidebar target')
        self.attempted = True  # Set before transport; uncertain input is never replayed.
        return self.vm.click(x,y,frame=frame)

    def key(self,*args,**kwargs): raise ValueError('this proof permits no keys')
    def type(self,*args,**kwargs): raise ValueError('this proof permits no typed text')


def save_result(directory,result):
    with private.private_output(directory/'proof.json') as output:
        output.write((json.dumps(result,sort_keys=True)+'\n').encode())


def request_barrier(directory):
    parent = os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        info = os.fstat(parent)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
            raise ProofRefusal('request barrier directory is unsafe')
        marker = os.open('request.started',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,
                         0o600,dir_fd=parent)
        try:
            os.fsync(marker)
        finally:
            os.close(marker)
        os.fsync(parent)
    finally:
        os.close(parent)


def run_once(vm,model_stage,guard,directory):
    directory = private.pinned.real_directory(Path(directory).resolve(strict=True),private=True)
    model_stage = Path(model_stage).resolve(strict=True)
    if model_stage in (directory,*directory.parents):
        raise ProofRefusal('the proof directory must be outside the writable model stage')
    if (directory/'request.started').exists() or (directory/'request.started').is_symlink():
        raise ProofRefusal('an earlier model request exists; automatic retry is refused')
    guarded = subprocess.run(['bash',str(guard),'--guard-offline'],capture_output=True,timeout=15)
    if guarded.returncode:
        raise ProofRefusal('host resource guard deferred the experiment')
    vm.health()
    preliminary = vm.frame()
    if preliminary.likely_blank:
        raise ProofRefusal('the guest frame is blank; this proof never wakes or unlocks it')
    before_evidence(vm.ocr(preliminary,accurate=True))
    proxy = SidebarOnlyVM(vm,directory)
    requested = False
    result = dict(schema='virtualos-finder-documents-proof/v1',task=TASK,
                  task_verified=False,operator_visual_review_required=True,
                  inference_requested=False,guest_input_attempted=False,
                  automatic_input_retry=False,private_backend_became_ready=False)
    try:
        with private.serve(model_stage,guard) as session:
            result['private_backend_became_ready'] = True
            def fetch(url):
                if url not in (session['endpoint']+'/api/tags',session['endpoint']+'/api/ps'):
                    raise OCRAdmissionError('only private model metadata is permitted')
                with brain._http('GET',url,timeout=5) as response:
                    body = response.read(65537)
                if len(body)>65536:
                    raise OCRAdmissionError('private model metadata exceeds bound')
                return json.loads(body)
            with admit(MODEL,session['endpoint'],1,1,fetch) as budget:
                def before_model():
                    nonlocal requested
                    session['recheck_global_idle']()
                    budget['recheck']()
                    session['recheck_global_idle']()
                    request_barrier(directory)
                    requested = True
                outcome = brain.run_brain(proxy,TASK,budget['model_reference'],session['endpoint'],
                    1,True,directory,'ocr',before_model)
                result.update(brain_result=outcome.to_json(),inference_requested=requested,
                              guest_input_attempted=proxy.attempted)
        result['private_backend_stopped_and_reaped'] = True
        if proxy.attempted:
            fresh = vm.frame()
            with private.private_output(directory/'after.jpg') as output:
                output.write(fresh.jpeg)
            semantic = not fresh.likely_blank and after_evidence(vm.ocr(fresh,accurate=True),proxy.baseline)
            actions = outcome.actions
            input_proven = (len(actions)==1 and actions[0].get('action')=='click'
                            and actions[0].get('http')==200 and actions[0].get('changed') is True)
            result.update(input_trace_http200_and_changed=input_proven,
                          independent_finder_documents_title_ocr=semantic,
                          independent_frame_changed=fresh.sha256!=proxy.frame_before.sha256,
                          frame_before_sha256=proxy.frame_before.sha256,frame_after_sha256=fresh.sha256,
                          semantic_candidate_verified=bool(input_proven and semantic
                              and fresh.sha256!=proxy.frame_before.sha256),
                          detected_sidebar_label='Documents',before_title=proxy.baseline['title']['text'])
    except Exception as failure:
        result.update(state='requires_attention',error_class=type(failure).__name__,
                      inference_requested=requested,guest_input_attempted=proxy.attempted)
        save_result(directory,result)
        raise
    result['state']='awaiting_operator_visual_review' if result.get('semantic_candidate_verified') else 'unverified'
    save_result(directory,result)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-once',action='store_true')
    parser.add_argument('--model-stage',type=Path,required=True)
    parser.add_argument('--guard',type=Path,required=True)
    parser.add_argument('--proof-directory',type=Path,required=True)
    args=parser.parse_args()
    try:
        if not args.execute_once:raise ProofRefusal('explicit execute-once required; no action performed')
        print(json.dumps(run_once(brain.FleetVM('vm-a',supervisor='http://127.0.0.1:9225'),
                                  args.model_stage,args.guard,args.proof_directory),sort_keys=True))
    except Exception as failure:
        print(json.dumps({'state':'refused','error_class':type(failure).__name__,'automatic_input_retry':False}))
        raise SystemExit(1)
