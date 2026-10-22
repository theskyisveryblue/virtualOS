from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import finder_documents_proof as proof


def row(text,x,y,width=.08,height=.02):
    return dict(text=text,x=x,y=y,width=width,height=height)


def entries(title='Recents'):
    return [row('Finder',.035,.96),row(title,.45,.82)]+[
        row(label,.2,y) for label,y in [('Recents',.73),('Applications',.68),
        ('Desktop',.63),('Documents',.58),('Downloads',.53)]]


def frame(body):
    return SimpleNamespace(jpeg=body,sha256=hashlib.sha256(body).hexdigest(),
                           width=1500,height=900,likely_blank=False)


class FinderProofTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='/private/tmp');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.root.chmod(0o700)
        self.modelstage=self.root/'model-stage';self.modelstage.mkdir(mode=0o700)

    def test_before_requires_foreground_finder_and_known_non_target_title(self):
        for rows in (entries('Documents'),entries('Unknown'),
                     [dict(r,text='Chrome') if r['text']=='Finder' else r for r in entries()]):
            with self.subTest(rows=rows),self.assertRaises(proof.ProofRefusal):
                proof.before_evidence(rows)

    def test_chrome_first_and_later_finder_text_is_not_foreground_finder(self):
        rows=[dict(r,text='Chrome') if r['text']=='Finder' else r for r in entries()]
        rows.append(row('Finder',.17,.96))
        with self.assertRaises(proof.ProofRefusal):proof.before_evidence(rows)

    def test_documents_without_sidebar_neighbors_is_not_a_target(self):
        rows=[row('Finder',.035,.96),row('Recents',.45,.82),row('Documents',.2,.58)]
        with self.assertRaises(proof.ProofRefusal):proof.before_evidence(rows)

    def test_duplicate_sidebar_targets_are_refused(self):
        rows=entries()+[row('Documents',.2,.48)]
        with self.assertRaises(proof.ProofRefusal):proof.before_evidence(rows)

    def test_independent_after_verification_requires_title_not_sidebar_text_only(self):
        before=proof.before_evidence(entries())
        self.assertFalse(proof.after_evidence(entries(),before))
        self.assertTrue(proof.after_evidence(entries('Documents'),before))
        self.assertFalse(proof.after_evidence([r for r in entries('Documents') if r['text']!='Finder'],before))

    def test_proxy_blocks_wrong_target_keys_text_and_replay(self):
        vm=Mock(name='vm-a');vm.name='vm-a';vm.ocr.return_value=entries()
        proxy=proof.SidebarOnlyVM(vm,self.root);before=frame(b'before')
        proxy.ocr(before)
        with self.assertRaises(ValueError):proxy.click(1,2,frame=before)
        for method in (proxy.key,proxy.type):
            with self.assertRaises(ValueError):method('anything')
        vm.click.assert_not_called()
        vm.click.return_value=200
        self.assertEqual(proxy.click(360,369,frame=before),200)
        with self.assertRaises(ValueError):proxy.click(360,369,frame=before)
        vm.click.assert_called_once()

    def run_trial(self,*,uncertain=False,already_target=False):
        before,after=frame(b'before'),frame(b'after')
        state={'clicked':False}
        vm=Mock();vm.name='vm-a'
        vm.frame.side_effect=lambda:after if state['clicked'] else before
        vm.ocr.side_effect=lambda image,**kw:entries('Documents' if state['clicked'] or already_target else 'Recents')
        def click(*args,**kwargs):
            state['clicked']=True
            if uncertain:raise OSError('uncertain delivery')
            return 200
        vm.click.side_effect=click
        recheck=Mock()
        @contextmanager
        def serve(*args):
            yield dict(endpoint='http://127.0.0.1:12345',recheck_global_idle=recheck)
        @contextmanager
        def admit(*args):
            yield dict(model_reference='sha256-'+'a'*64,recheck=recheck)
        with patch.object(proof.subprocess,'run',return_value=SimpleNamespace(returncode=0)), \
             patch.object(proof.private,'serve',side_effect=serve) as server, \
             patch.object(proof,'admit',side_effect=admit), \
             patch.object(proof.brain,'_ollama_ocr_action',return_value={
                 'type':'click','point_2d':[240,410],'label':'Documents'}) as model, \
             patch.object(proof.brain.time,'sleep'):
            if already_target:
                with self.assertRaises(proof.ProofRefusal):
                    proof.run_once(vm,self.modelstage,Path('/guard'),self.root)
                server.assert_not_called();model.assert_not_called();vm.click.assert_not_called()
                return
            result=proof.run_once(vm,self.modelstage,Path('/guard'),self.root)
        self.assertEqual(model.call_count,1)
        self.assertEqual(vm.click.call_count,1)
        self.assertTrue(result['inference_requested'])
        self.assertTrue((self.root/'request.started').exists())
        with self.assertRaises(proof.ProofRefusal):
            proof.run_once(vm,self.modelstage,Path('/guard'),self.root)
        return result

    def test_one_action_produces_candidate_evidence_but_never_self_verifies_task(self):
        result=self.run_trial()
        self.assertTrue(result['semantic_candidate_verified'])
        self.assertTrue(result['input_trace_http200_and_changed'])
        self.assertTrue(result['independent_finder_documents_title_ocr'])
        self.assertFalse(result['task_verified'])
        self.assertFalse(result['brain_result']['task_verified'])
        self.assertEqual(result['state'],'awaiting_operator_visual_review')

    def test_uncertain_delivery_is_unverified_and_never_replayed(self):
        result=self.run_trial(uncertain=True)
        self.assertFalse(result['semantic_candidate_verified'])
        self.assertEqual(result['brain_result']['status'],'input_uncertain')
        self.assertFalse(result['task_verified'])

    def test_already_open_target_never_starts_server_or_requests_model(self):
        self.run_trial(already_target=True)

    def test_fresh_admission_refusal_never_requests_model_or_inputs(self):
        image=frame(b'before');vm=Mock();vm.name='vm-a'
        vm.frame.return_value=image;vm.ocr.return_value=entries()
        @contextmanager
        def serve(*args):
            yield dict(endpoint='http://127.0.0.1:12345',recheck_global_idle=Mock())
        @contextmanager
        def admitted(*args):
            yield dict(model_reference='sha256-'+'a'*64,
                       recheck=Mock(side_effect=proof.OCRAdmissionError('budget changed')))
        with patch.object(proof.subprocess,'run',return_value=SimpleNamespace(returncode=0)), \
             patch.object(proof.private,'serve',side_effect=serve), \
             patch.object(proof,'admit',side_effect=admitted), \
             patch.object(proof.brain,'_ollama_ocr_action') as model:
            result=proof.run_once(vm,self.modelstage,Path('/guard'),self.root)
        self.assertFalse(result['inference_requested'])
        self.assertFalse(result['guest_input_attempted'])
        self.assertFalse((self.root/'request.started').exists())
        model.assert_not_called();vm.click.assert_not_called()

    def test_model_writable_directory_cannot_hold_input_evidence(self):
        nested=self.root/'inside';nested.mkdir(mode=0o700)
        with self.assertRaises(proof.ProofRefusal):
            proof.run_once(Mock(),self.root,Path('/guard'),nested)

    def test_relative_and_parent_aliases_cannot_bypass_evidence_separation(self):
        inner=self.modelstage/'inside';inner.mkdir(mode=0o700)
        alias=self.modelstage/'..'/'model-stage'
        with self.assertRaises(proof.ProofRefusal):
            proof.run_once(Mock(),alias,Path('/guard'),inner)
        cwd=Path.cwd()
        try:
            proof.os.chdir(self.root)
            with self.assertRaises(proof.ProofRefusal):
                proof.run_once(Mock(),Path('model-stage'),Path('/guard'),Path('model-stage/inside'))
        finally:proof.os.chdir(cwd)

    def test_request_barrier_fsyncs_file_and_directory_before_returning(self):
        with patch.object(proof.os,'fsync',wraps=proof.os.fsync) as sync:
            proof.request_barrier(self.root)
        self.assertEqual(sync.call_count,2)
        self.assertTrue((self.root/'request.started').exists())
        with self.assertRaises(FileExistsError):proof.request_barrier(self.root)


if __name__=='__main__':unittest.main()
