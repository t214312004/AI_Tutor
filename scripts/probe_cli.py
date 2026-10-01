"""Synthetic-only integration probe; no student files, camera or microphone input."""
import asyncio
import json
import base64
import sys
from uuid import uuid4
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server.providers import CodexClient, AgyClient, AgyTutor, LazyCodex, ProviderError
from server.basic_workspace import BasicWorkspace

async def main():
    workspace=Path('.local/cli-diagnostic').resolve();workspace.mkdir(parents=True,exist_ok=True)
    if '--sandbox-readiness' in sys.argv:
        basic=BasicWorkspace(workspace/f'readiness-{uuid4().hex}')
        client=CodexClient(basic.root)
        client.strict_permissions=True
        try:
            try:await client.start()
            except ProviderError as error:
                print(json.dumps({'provider':'codex','windows_sandbox_readiness':client.windows_sandbox_readiness,
                                  'error':str(error)},ensure_ascii=False))
                return
            print(json.dumps({'provider':'codex','windows_sandbox_readiness':client.windows_sandbox_readiness}))
        finally:
            await client.close()
        return
    if '--legacy-policy' in sys.argv:
        basic=BasicWorkspace(workspace/f'legacy-policy-{uuid4().hex}')
        client=CodexClient(basic.root)
        target=basic.root/'scratch-probe.txt'
        try:
            await client.start()
            effective=await client.request('config/read',{})
            requirements=await client.request('configRequirements/read',{})
            result=await client.request('command/exec',{
                'command':['powershell.exe','-NoProfile','-Command',
                           'Set-Content -LiteralPath "'+str(target)+'" -Value "synthetic"'],
                'cwd':str(basic.root),
                'sandboxPolicy':{'type':'workspaceWrite','writableRoots':[str(basic.root)],'networkAccess':False},
                'timeoutMs':15000},timeout=20)
            print(json.dumps({'provider':'codex','policy':'workspaceWrite',
                              'exit_code':result.get('exitCode'),'file_created':target.exists(),
                              'stderr':result.get('stderr','')[:400],
                              'config_sandbox':effective.get('config',{}).get('sandbox_mode'),
                              'default_permissions':effective.get('config',{}).get('default_permissions'),
                              'shell_tool':effective.get('config',{}).get('features',{}).get('shell_tool'),
                              'unified_exec':effective.get('config',{}).get('features',{}).get('unified_exec'),
                              'requirements':requirements.get('requirements')},ensure_ascii=False))
        except ProviderError as error:
            print(json.dumps({'provider':'codex','policy':'workspaceWrite','error':str(error)},ensure_ascii=False))
            raise SystemExit(1) from None
        finally:
            await client.close()
            target.unlink(missing_ok=True)
        return
    if '--direct-codex-write' in sys.argv:
        basic=BasicWorkspace(workspace/f'direct-codex-{uuid4().hex}')
        client=CodexClient(basic.root)
        target=basic.root/'scratch-note.txt'
        try:
            await client.start()
            reply=await client.turn('這是合成陪讀課程。請用檔案工具在工作目錄建立 scratch-note.txt，'
                                    '只寫入「7 + 5 練習湊十」，供下回合接續使用。')
            print(json.dumps({'provider':'codex','startup_check':False,
                              'file_created':target.exists(),
                              'contents_correct':target.exists() and target.read_text(encoding='utf-8').strip()=='7 + 5 練習湊十',
                              'reply':reply[:350]},ensure_ascii=False))
        finally:
            await client.close()
            target.unlink(missing_ok=True)
        return
    if '--thread-policy-matrix' in sys.argv:
        basic=BasicWorkspace(workspace/f'thread-policy-{uuid4().hex}')
        client=CodexClient(basic.root)
        try:
            await client.start()
            cases={
                'legacy_ephemeral':{'sandbox':'workspace-write','ephemeral':True},
                'legacy_persistent':{'sandbox':'workspace-write','ephemeral':False},
                'read_only_control':{'sandbox':'read-only','ephemeral':True},
                'built_in_workspace':{'permissions':':workspace','ephemeral':True},
                'actual_config':{'sandbox':'workspace-write','ephemeral':True,
                                 'config':{'features.shell_tool':True,'features.view_image':True,
                                           'features.apps':False,'web_search':'disabled'}},
            }
            outcomes={}
            for name,overrides in cases.items():
                try:
                    result=await client.request('thread/start',{
                        'cwd':str(basic.root),'approvalPolicy':'never',**overrides})
                    outcomes[name]={'sandbox':result.get('sandbox'),
                                    'permission_profile':result.get('activePermissionProfile'),
                                    'workspace_roots':result.get('runtimeWorkspaceRoots')}
                except ProviderError as error:outcomes[name]={'error':str(error)}
            print(json.dumps({'provider':'codex','cases':outcomes},ensure_ascii=False))
        finally:
            await client.close();await basic.close()
        return
    if '--profile' in sys.argv:
        class_workspace=BasicWorkspace(workspace/f'profile-{uuid4().hex}')
        sibling=workspace/f'profile-sibling-{uuid4().hex}'
        sibling.mkdir()
        marker='SYNTHETIC-'+uuid4().hex
        (class_workspace.root/'readable.txt').write_text(marker,encoding='utf-8')
        (sibling/'private.txt').write_text(marker,encoding='utf-8')
        client=CodexClient(class_workspace.root)
        client.strict_permissions=True
        try:
            try:await client.start()
            except ProviderError as error:
                print(json.dumps({'provider':'codex','tutor_profile_ready':False,
                                  'windows_sandbox_readiness':client.windows_sandbox_readiness,
                                  'error':str(error)},ensure_ascii=False))
                raise SystemExit(1) from None
            profiles=await client.request('permissionProfile/list',{'cwd':str(class_workspace.root)})
            tutor=next((item for item in profiles.get('data',[]) if item.get('id')=='tutor'),None)
            async def read(path):
                command=['powershell.exe','-NoProfile','-Command',
                         'Get-Content -LiteralPath "'+str(path)+'"']
                return await client.request('command/exec',{'command':command,'cwd':str(class_workspace.root),
                                                            'permissionProfile':'tutor','timeoutMs':15000},timeout=20)
            own=await read(class_workspace.root/'readable.txt')
            other=await read(sibling/'private.txt')
            async def write(path):
                return await client.request('command/exec',{
                    'command':['powershell.exe','-NoProfile','-Command',
                               'Set-Content -LiteralPath "'+str(path)+'" -Value "synthetic"'],
                    'cwd':str(class_workspace.root),'permissionProfile':'tutor',
                    'timeoutMs':15000},timeout=20)
            note=class_workspace.root/'scratch-probe.txt'
            protected_guide=class_workspace.root/'AGENTS.md'
            protected_photo=class_workspace.photos/'protected-probe.txt'
            note_result=await write(note)
            original_guide=protected_guide.read_text(encoding='utf-8')
            guide_result=await write(protected_guide)
            photo_result=await write(protected_photo)
            allowed=bool(tutor and tutor.get('allowed'))
            own_ok=own.get('exitCode')==0 and marker in own.get('stdout','')
            sibling_denied=marker not in other.get('stdout','')
            note_written=note.exists() and note.read_text(encoding='utf-8').strip()=='synthetic'
            guide_protected=protected_guide.read_text(encoding='utf-8')==original_guide
            photo_protected=not protected_photo.exists()
            print(json.dumps({'provider':'codex','tutor_profile_available':allowed,
                              'own_read':own_ok,'sibling_denied':sibling_denied,
                              'own_exit':own.get('exitCode'),'sibling_exit':other.get('exitCode'),
                              'note_written':note_written,'guide_protected':guide_protected,
                              'photo_protected':photo_protected,
                              'note_exit':note_result.get('exitCode'),
                              'guide_exit':guide_result.get('exitCode'),
                              'photo_exit':photo_result.get('exitCode'),
                              'sibling_stderr':other.get('stderr','')[:160]}))
            if not (allowed and own_ok and sibling_denied and note_written and guide_protected and photo_protected):
                raise SystemExit(1)
        finally:
            await client.close()
            (class_workspace.root/'scratch-probe.txt').unlink(missing_ok=True)
            (class_workspace.photos/'protected-probe.txt').unlink(missing_ok=True)
            (sibling/'private.txt').unlink(missing_ok=True)
            sibling.rmdir()
        return
    if '--workspace-write' in sys.argv:
        backend='agy' if '--agy' in sys.argv else 'codex'
        class_workspace=BasicWorkspace(workspace/f'workspace-write-{backend}-{uuid4().hex}')
        tutor=AgyTutor(class_workspace.root) if backend=='agy' else LazyCodex(class_workspace.root)
        if backend=='codex':
            class ProbeAudit:
                def __init__(self):self.items=[];self.policy=None
                def record(self,event,**data):
                    if event=='codex_item' and data.get('method')=='item/completed':
                        self.items.append(data.get('params',{}).get('item',{}))
                    if event=='codex_thread_ready':self.policy=data
            probe_audit=ProbeAudit()
            tutor.client.audit=probe_audit
        note=class_workspace.root/'scratch-note.txt'
        verified=False
        try:
            await tutor.ensure_ready()
            reply=await tutor.turn('這是合成的陪讀課程。請在目前學生工作目錄建立 scratch-note.txt，'
                                   '只寫入一行「7 + 5 練習湊十」。這是供下一回合接續使用的必要陪讀筆記。'
                                   '請實際使用可用的檔案或 shell 工具，不要只口頭回答；完成後簡短回報。')
            verified=note.exists() and note.read_text(encoding='utf-8').strip()=='7 + 5 練習湊十'
            print(json.dumps({'provider':backend,'file_created':note.exists(),
                              'contents_correct':verified,'reply':reply[:350],
                              'effective_policy':probe_audit.policy if backend=='codex' else None,
                              'tool_items':[str(item)[:700] for item in probe_audit.items if item.get('type')!='agentMessage']
                              if backend=='codex' else []},ensure_ascii=False))
        finally:
            try:await tutor.close()
            finally:
                note.unlink(missing_ok=True)
                await class_workspace.close()
        if not verified:raise SystemExit(1)
        return
    if '--workspace-photo' in sys.argv:
        from server.tutor import TutorSession,TutorEngine
        backend='agy' if '--agy' in sys.argv else 'codex'
        photo_workspace=BasicWorkspace(workspace/f'workspace-photo-{backend}-{uuid4().hex}')
        tutor=AgyTutor(photo_workspace.root) if backend=='agy' else LazyCodex(photo_workspace.root)
        if backend=='codex':
            class ImageAudit:
                def __init__(self):self.paths=[]
                def record(self,event,**data):
                    if event=='codex_image_view':self.paths.append(data.get('path'))
            image_audit=ImageAudit()
            tutor.client.audit=image_audit
        try:
            await tutor.ensure_ready()
            image='data:image/jpeg;base64,'+base64.b64encode(Path('test-results/synthetic-worksheet.jpg').read_bytes()).decode()
            notice=await photo_workspace.publish(image)
            session=TutorSession('synthetic',2,basic_workflow=True)
            session.camera_enabled=True
            result=await TutorEngine(tutor).respond(session,'照片更新',observation=True,photo_notice=notice)
            print(json.dumps({'provider':backend,'observation':result,'photo_notice':notice,
                              'image_view_paths':image_audit.paths if backend=='codex' else None},ensure_ascii=False))
        finally:
            await tutor.close();await photo_workspace.close()
        return
    if '--basic-tutor' in sys.argv:
        from server.tutor import TutorSession,TutorEngine
        backend='agy' if '--agy' in sys.argv else 'codex'
        basic=BasicWorkspace(workspace/f'basic-tutor-{backend}-{uuid4().hex}')
        tutor=AgyTutor(basic.root) if backend=='agy' else LazyCodex(basic.root)
        try:
            await tutor.ensure_ready()
            session=TutorSession('synthetic',2,basic_workflow=True)
            session.workspace_context=basic.continuity()
            engine=TutorEngine(tutor)
            first=await engine.respond(session,'我想練習 7 + 5，請只給我下一步提示。')
            first_id=tutor.conversation_id if backend=='agy' else tutor.client.thread_id
            second=await engine.respond(session,'我想到先把 7 補成 10，接下來呢？')
            print(json.dumps({'provider':backend,
                              'first_speech':first.get('speech') if first else None,
                              'first_board':bool(first and first.get('board_html')),
                              'second_speech':second.get('speech') if second else None,
                              'same_native_conversation':bool(first_id and first_id==(tutor.conversation_id if backend=='agy' else tutor.client.thread_id))},
                             ensure_ascii=False))
        finally:
            await tutor.close();await basic.close()
        return
    if '--continuity' in sys.argv:
        marker='TEST-'+uuid4().hex[:10]
        if '--agy' in sys.argv:
            tutor=AgyTutor(workspace/'agy-continuity')
            try:
                await tutor.ensure_ready()
                ident=tutor.conversation_id
                first=await tutor.turn(f'這是合成測試。請記住代號 {marker}，只回覆「收到」。')
                if '--vision' in sys.argv:
                    image='data:image/png;base64,'+base64.b64encode(Path('test-results/synthetic-worksheet.png').read_bytes()).decode()
                    await tutor.turn('請辨識這張人工測試圖片，只回覆看到的算式。',image_url=image)
                second=await tutor.turn('剛才要記住的代號是什麼？只回覆代號。')
                print(json.dumps({'provider':'agy','same_session':ident==tutor.conversation_id,
                                  'remembered':marker in second,'interleaved_image':'--vision' in sys.argv,
                                  'first_reply':first[:80],
                                  'second_reply':second[:80]},ensure_ascii=False))
            finally:await tutor.close()
            return
        tutor=LazyCodex(workspace/'codex-continuity')
        try:
            await tutor.ensure_ready()
            ident=tutor.client.thread_id
            first=await tutor.turn(f'這是合成測試。請記住代號 {marker}，只回覆「收到」。')
            if '--vision' in sys.argv:
                image='data:image/png;base64,'+base64.b64encode(Path('test-results/synthetic-worksheet.png').read_bytes()).decode()
                await tutor.turn('請辨識這張人工測試圖片，只回覆看到的算式。',image_url=image)
            second=await tutor.turn('剛才要記住的代號是什麼？只回覆代號。')
            print(json.dumps({'provider':'codex','same_session':ident==tutor.client.thread_id,
                              'remembered':marker in second,'interleaved_image':'--vision' in sys.argv,
                              'first_reply':first[:80],
                              'second_reply':second[:80]},ensure_ascii=False))
        finally:await tutor.close()
        return
    if '--agy' in sys.argv:
        if '--tutor' in sys.argv:
            from server.tutor import TutorSession,TutorEngine
            tutor=AgyTutor(workspace/'agy-tutor')
            await tutor.ensure_ready()
            session=TutorSession('synthetic',2)
            session.basic_workflow=True
            image=('data:image/png;base64,'+base64.b64encode(Path('test-results/synthetic-worksheet.png').read_bytes()).decode()
                   if '--vision' in sys.argv else None)
            session.camera_enabled=bool(image)
            reply=await TutorEngine(tutor).respond(session,
                '請只記錄圖片中可見的作業內容。' if image else '7 + 5 要怎麼算？請提示，不要直接告訴我答案。',
                image_url=image,observation=bool(image))
            print(json.dumps({'provider':'agy','tutor':reply},ensure_ascii=False));return
        result=await AgyClient().turn('Do not use any tools or files. Reply with exactly TUTOR_OK.',workspace)
        print(json.dumps({'provider':'agy','result':result},ensure_ascii=False));return
    if '--ready' in sys.argv:
        tutor=LazyCodex(workspace/'codex-ready')
        try:
            await tutor.ensure_ready()
            print(json.dumps({'provider':'codex','ready':True}))
        finally:await tutor.close()
        return
    client=CodexClient(workspace)
    try:
        await client.start();print(json.dumps(await client.account_status()))
        text=await client.turn('Do not use any tools or files. Reply with exactly TUTOR_OK.')
        print(json.dumps({'provider':'codex','text':text},ensure_ascii=False))
        if '--vision' in sys.argv and '--tutor' not in sys.argv:
            image='data:image/png;base64,'+base64.b64encode(Path('test-results/synthetic-worksheet.png').read_bytes()).decode()
            text=await client.turn('Read the arithmetic equation in the provided synthetic test image. Return only the equation.',image_url=image)
            print(json.dumps({'vision':text},ensure_ascii=False))
        if '--tutor' in sys.argv:
            from server.tutor import TutorSession,TutorEngine
            session=TutorSession('synthetic',2)
            session.basic_workflow=True
            image=('data:image/png;base64,'+base64.b64encode(Path('test-results/synthetic-worksheet.png').read_bytes()).decode()
                   if '--vision' in sys.argv else None)
            session.camera_enabled=bool(image)
            reply=await TutorEngine(client).respond(session,
                '請只記錄圖片中可見的作業內容。' if image else '7 + 5 要怎麼算？請提示，不要直接告訴我答案。',
                image_url=image,observation=bool(image))
            print(json.dumps({'tutor':reply},ensure_ascii=False))
    finally:await client.close()

if __name__=='__main__':asyncio.run(main())
