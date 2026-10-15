#!/usr/bin/env python3
"""Capture only generated notification pixels in a marked, seat-disabled lab."""
import argparse
import json
from pathlib import Path
import signal
import subprocess
import time

from lab import PROJECT
from render_assets_smoke import RenderAssetsSmoke
from verify_capture import absence_report, color_fraction

SECRET = (248, 16, 184)
CARDS = ((700, 40, 280, 80), (750, 140, 230, 100))


class NotificationSmoke(RenderAssetsSmoke):
    def __init__(self, args):
        super().__init__(args)
        self.args.suite = 'notifications'
        self.report['scope'] = 'generated layer-shell cards and a secret outside their input mask; no personal notification data'

    def start(self):
        super().start()
        self.ctl('output', 'create', 'headless', 'HV-NOTIFICATIONS')
        config = self.runtime / 'hyprland.lua'
        additions = '\n'.join('hl.monitor({output=' + json.dumps(m['name']) + ',disabled=true})'
                              for m in json.loads(self.ctl('-j', 'monitors')) if m['name'].startswith('WAYLAND-'))
        config.write_text(config.read_text() + '\n' + additions + '\n')
        self.ctl('reload')

    def monitor(self):
        return next(m for m in json.loads(self.ctl('-j', 'monitors'))
                    if m['name'] == 'HV-NOTIFICATIONS' and m['width'] > 0)

    def capture(self, path):
        from lab import load_lab
        load_lab(self.runtime)
        log = (self.artifacts / (path.stem + '-wire.log')).open('w')
        try:
            subprocess.run(['grim', '-o', self.monitor()['name'], str(path)],
                           env=dict(self.env, WAYLAND_DEBUG='1'), stdout=subprocess.DEVNULL,
                           stderr=log, timeout=15, check=True)
        finally:
            log.close()

    def notification_fixture(self, full=False):
        path = self.runtime / ('full.qml' if full else 'cards.qml')
        mask = 'mask: Region { item: root.contentItem }' if full else '''mask: Region {
          regions: [Region { x:700; y:40; width:280; height:80 },
                    Region { x:750; y:140; width:230; height:100 }]
        }'''
        path.write_text('''import QtQuick
import Quickshell
import Quickshell.Wayland
ShellRoot {
 PanelWindow {
  id: root
  anchors { top:true; bottom:true; left:true; right:true }
  color: "transparent"
  exclusionMode: ExclusionMode.Ignore
  WlrLayershell.namespace: "omarchy-notifications"
  WlrLayershell.layer: WlrLayer.Overlay
  WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
  ''' + mask + '''
  Rectangle { x:700; y:40; width:280; height:80; color:"#f810b8" }
  Rectangle { x:750; y:140; width:230; height:100; color:"#f810b8" }
  // Deliberately private pixels outside the claimed input envelopes.
  Rectangle { x:30; y:600; width:80; height:80; color:"#f810b8" }
 }
}
''')
        log = (self.artifacts / path.with_suffix('.log').name).open('w')
        self.logs.append(log)
        env = dict(self.env, QT_QPA_PLATFORM='wayland', QT_QPA_PLATFORMTHEME='', QT_STYLE_OVERRIDE='Fusion')
        process = subprocess.Popen(['quickshell', '-p', str(path)], env=env,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        self.fixtures.append(process)
        time.sleep(.6)
        if process.poll() is not None:
            raise RuntimeError('notification fixture exited; see ' + str(log.name))
        return process

    def stop_fixture(self, process):
        process.terminate()
        process.wait(timeout=5)
        self.fixtures.remove(process)
        time.sleep(.2)

    def tests(self):
        output = self.monitor()['name']
        config = self.runtime / 'hyprland.lua'
        config.write_text(config.read_text() + '\nhl.monitor({output=' + json.dumps(output) + ',mode="1024x768@60",position="0x0",scale=1})\n')
        self.ctl('reload'); time.sleep(.4)
        self.fixture('background')
        self.popup_fixture()
        self.dispatch('hl.dsp.window.tag({window=' + json.dumps(self.address()) + ',tag="+hyprveil-private-test"})')
        self.capture_image('before-plugin')
        self.load(); self.ctl('dismissnotify', '-1')
        self.before = self.geometry()
        self.patch({'mode':'color', 'mask_color':'#152535'})
        self.frame('baseline-no-notifications')
        self.patch({'mode':'spoiler', 'variant':'error404', 'speed':0, 'eye':False,
                    'notification_enabled':True, 'notification_mode':'color',
                    'notification_mask_color':'#123456', 'notification_rounding':0})
        process = self.notification_fixture()
        self.report['before_notification_capture'] = self.status()
        self.save()
        image = self.frame('separate-color')
        self.assert_check('all notification source pixels omitted including outside input region',
                          absence_report(image, SECRET, 8)['ok'])
        self.assert_check('both bounded cards use independent color',
                          all(color_fraction(image, (x+4,y+4,w-8,h-8), (18,52,86),2)>.99 for x,y,w,h in CARDS),
                          status=self.status()['notifications'])
        self.assert_check('gap and unrelated monitor area remain background',
                          color_fraction(image,(710,124,20,12),(36,132,196),2)>.99 and
                          color_fraction(image,(30,600,80,80),(36,132,196),2)>.99)
        self.assert_check('window remains 404 with its own settings',
                          self.status()['mode']=='spoiler' and self.status()['appearance']['variant']=='error404')
        self.patch({'notification_mode':'omit'})
        omitted = self.frame('notification-omit')
        self.assert_check('omit removes cards without filling the monitor',
                          all(color_fraction(omitted,rect,(36,132,196),2)>.99 for rect in CARDS))
        self.patch({'notification_mode':'spoiler','notification_variant':'matrix',
                    'notification_speed':100,'notification_eye':False})
        first = self.frame('notification-matrix')
        animation_status = self.last_capture_status
        time.sleep(.12)
        second = self.frame('notification-matrix-next')
        from verify_capture import differing_fraction
        difference = differing_fraction(first, CARDS[0], second, CARDS[0], 2)
        self.assert_check('notification animation advances independently', difference > .01,
                          changed_fraction=difference, capture_status=animation_status)
        broken = self.runtime / 'notification-broken.frag'; broken.write_text('invalid GLSL\n')
        self.patch({'notification_shader_path':str(broken)})
        fallback = self.frame('notification-shader-fallback')
        self.assert_check('invalid notification shader falls back to independent opaque color',
                          all(color_fraction(fallback,(x+4,y+4,w-8,h-8),(18,52,86),2)>.99 for x,y,w,h in CARDS))
        self.stop_fixture(process)
        self.patch({'notification_mode':'color'})
        process = self.notification_fixture(full=True)
        full = self.frame('notification-fullscreen-region-refused')
        self.assert_check('fullscreen input mask omits layer without fullscreen fill',
                          absence_report(full,SECRET,8)['ok'] and
                          color_fraction(full,(700,40,280,80),(36,132,196),2)>.99 and
                          self.status()['notifications']['geometry_status'].startswith('omitted'))
        self.stop_fixture(process)
        self.report['native_status'] = self.status()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent-runtime',required=True)
    parser.add_argument('--parent-display',required=True)
    parser.add_argument('--plugin',type=Path,default=PROJECT/'build/hyprveil.so')
    run=NotificationSmoke(parser.parse_args())
    def interrupted(signum, frame): raise KeyboardInterrupt('notification smoke interrupted')
    signal.signal(signal.SIGINT,interrupted); signal.signal(signal.SIGTERM,interrupted)
    try:
        run.start(); run.tests(); run.report['ok']=all(check['ok'] for check in run.checks)
    except BaseException as error:
        run.report.update(error=str(error),ok=False)
    finally:
        run.cleanup(); run.save()
    print(json.dumps(dict(ok=run.report['ok'],checks=len(run.checks),report=str(run.artifacts/'report.json') if run.artifacts else None)),flush=True)
    return 0 if run.report['ok'] else 1

if __name__=='__main__': raise SystemExit(main())
