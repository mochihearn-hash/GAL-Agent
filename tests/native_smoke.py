"""Explicit native smoke test. Creates and controls only its own test window."""
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))


def fixture(directory):
    import tkinter as tk
    from ctypes import wintypes as W
    user=ctypes.WinDLL('user32')
    user.SetProcessDpiAwarenessContext.argtypes=[W.HANDLE]
    user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    user.GetAncestor.argtypes=[W.HWND,W.UINT];user.GetAncestor.restype=W.HWND
    root=tk.Tk();root.title('GAL Agent Native Test Fixture');root.geometry('800x600+80+80');root.configure(bg='#263d48')
    canvas=tk.Canvas(root,width=800,height=600,highlightthickness=0,bg='#263d48');canvas.pack(fill='both',expand=True)
    stage=0
    def draw():
        canvas.delete('all');canvas.create_rectangle(0,0,800,600,fill=['#263d48','#405664','#614d35'][min(stage,2)],outline='')
        canvas.create_text(400,110,text='NATIVE CAPTURE FIXTURE',font=('Arial',22),fill='#ece6d4')
        canvas.create_rectangle(35,350,765,565,fill='#152027',outline='#a88e61',width=2)
        canvas.create_text(65,385,anchor='nw',text=['A promise at the station. Press SPACE to continue.','Choose: trust the promise, or walk away.','You chose to trust. The next chapter begins.'][min(stage,2)],font=('Arial',16),fill='white',width=660)
        if stage==1:
            canvas.create_rectangle(250,220,550,270,fill='#b49461',outline='')
            canvas.create_text(400,245,text='Trust the promise',font=('Arial',15),fill='#17252c')
        (directory/'fixture-state.json').write_text(json.dumps({'stage':stage}),encoding='utf8')
    def advance(event=None):
        nonlocal stage
        stage+=1;draw()
    root.bind('<space>',advance)
    canvas.bind('<Button-1>',lambda event:advance() if stage==1 and 250<=event.x<=550 and 220<=event.y<=270 else None)
    draw();root.update();root.lift();root.focus_force()
    hwnd=int(user.GetAncestor(root.winfo_id(),2))
    print(json.dumps({'hwnd':hwnd,'pid':os.getpid()}),flush=True)
    def check_stop():
        if (directory/'fixture-stop').exists():root.destroy()
        else:root.after(150,check_stop)
    root.after(150,check_stop)
    root.mainloop()


def run():
    from app.capture.windows import Windows
    from app.capture.wgc import Capture,ResizeRequired
    from app.execution.executor import Executor
    from app.schemas import Action,GameProfile
    directory=ROOT/'test-results'/'native';directory.mkdir(parents=True,exist_ok=True)
    (directory/'fixture-stop').unlink(missing_ok=True)
    child=subprocess.Popen([sys.executable,__file__,'--fixture',str(directory)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,creationflags=subprocess.CREATE_NO_WINDOW)
    capture=None
    try:
        ready=json.loads(child.stdout.readline())
        windows=Windows();info=windows.info(ready['hwnd'])
        if info.pid!=ready['pid']:raise RuntimeError('Fixture identity mismatch')
        capture=Capture(windows);capture.bind(info)
        def shot():
            deadline=time.monotonic()+8
            last=''
            while time.monotonic()<deadline:
                try:return capture.snapshot()
                except ResizeRequired as exc:capture.rebind_if_current(exc.target)
                except ValueError as exc:last=str(exc);time.sleep(.15)
            raise RuntimeError(f'No valid native capture: {last}; frame={windows.info(info.hwnd).frame}; buffer={None if capture._buffer is None else capture._buffer.shape}')
        image=shot();image.image.save(directory/'capture.png')
        assert image.image.size==(info.client[2],info.client[3])
        user=windows.user
        user.SetForegroundWindow.argtypes=[ctypes.c_void_p];user.SetForegroundWindow.restype=ctypes.c_int
        user.SetForegroundWindow(info.hwnd)
        time.sleep(.2)
        executor=Executor(windows)
        input_result='skipped: Windows did not grant foreground focus'
        if windows.foreground(info.hwnd):
            executor.execute(shot(),Action(type='advance'),[],GameProfile(),lambda:True)
            time.sleep(.5)
            assert json.loads((directory/'fixture-state.json').read_text())['stage']==1
            executor.execute(shot(),Action(type='click',x=.5,y=245/600),[],GameProfile(),lambda:True)
            time.sleep(.5)
            assert json.loads((directory/'fixture-state.json').read_text())['stage']==2
            input_result='passed on owned fixture'
        # Geometry updates must use a fresh frame; stale observations are rejected.
        stale=shot()
        user.MoveWindow.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int]
        user.MoveWindow(info.hwnd,130,120,916,738,1)
        time.sleep(.5)
        try:executor.check(stale,Action(type='click',x=.5,y=.5),[],GameProfile(),lambda:True)
        except ValueError:pass
        else:raise AssertionError('Stale geometry accepted')
        resized=shot();resized.image.save(directory/'resized-capture.png')
        assert resized.image.size!=image.image.size
        report={'capture':'passed','binding':'exact HWND + PID + process creation time','keyboard':input_result,'mouse':input_result,'resize':'passed; stale geometry rejected','initial_size':image.image.size,'resized_size':resized.image.size,'real_game':False,'real_model':False}
        (directory/'report.json').write_text(json.dumps(report,indent=2),encoding='utf8')
        print(json.dumps(report))
    finally:
        if capture:capture.close()
        (directory/'fixture-stop').touch()
        try:child.wait(timeout=3)
        except subprocess.TimeoutExpired:child.terminate()


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--fixture':fixture(Path(sys.argv[2]))
    else:run()
