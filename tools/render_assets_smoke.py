#!/usr/bin/env python3
"""Exercise user backgrounds, procedural GLSL and icons in a guarded lab only."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import signal
import sys
import time
import cairo
from lab import PROJECT
from native_config_smoke import lua_literal
from spoiler_smoke import SpoilerSmoke, PRIVATE
from verify_capture import absence_report, color_fraction, opacity_fraction, differing_fraction

class RenderAssetsSmoke(SpoilerSmoke):
    def __init__(self, args):
        args.force_shader_failure = False
        super().__init__(args)
        self.args.suite = 'render-assets'
        self.report['scope'] = 'physically seat-disabled lab, generated local PNG/GLSL assets; no third-party plugin installed or loaded'

    def patch(self, values):
        result = self.ctl('repl', 'local v,e=hl.plugin.hyprveil.configure(' + lua_literal(values) + '); if not v then error(e) end; return v.mode')
        if result != self.status()['mode']:
            raise RuntimeError('native settings acknowledgement mismatch')

    def monitor(self):
        return next(m for m in json.loads(self.ctl('-j', 'monitors'))
                    if m['name'].startswith('WAYLAND-') and m['width'] > 0)

    def capture(self, path):
        self.command(['grim', '-o', self.monitor()['name'], str(path)])

    def status(self):
        return json.loads(self.ctl('hyprveil', 'status'))

    def frame(self, name):
        image = self.capture_image(name)
        self.last_capture_status = self.status()
        self.assert_check(name + ': no private pixels, opaque export, stable local window',
                          absence_report(image, PRIVATE, 8)['ok'] and
                          opacity_fraction(image, (0, 0, image.width, image.height)) == 1 and
                          self.geometry() == self.before and self.privacy())
        return image

    def point(self, image, x, y):
        origin = self.protected()['at']
        return next(image.pixels((origin[0] + x, origin[1] + y, 1, 1)))

    def tests(self):
        output = self.monitor()['name']
        config = self.runtime / 'hyprland.lua'
        config.write_text(config.read_text() + '\nhl.monitor({output=' + json.dumps(output) + ',mode="1024x768@60",position="0x0",scale=1})\n')
        self.ctl('reload')
        time.sleep(.4)
        self.fixture('background')
        self.popup_fixture()
        self.dispatch('hl.dsp.window.tag({window=' + json.dumps(self.address()) + ',tag="+hyprveil-private-test"})')
        self.load()
        self.ctl('dismissnotify', '-1')
        self.before = self.geometry()
        canonical = self.ctl('repl', 'hl.config({plugin={hyprveil={mask_color="#ABCDEF"}}}); return hl.plugin.hyprveil.status().mask_color')
        self.assert_check('native Lua parser canonicalizes mask_color before status and partial-save comparison', canonical == '#abcdef')
        self.patch({'mode':'color', 'mask_color':'#152535'})
        fill = self.frame('color-mask')
        self.assert_check('color mode uses chosen opaque RGB', color_fraction(fill, self.roi(), (21,37,53), 1) > .99)
        self.patch({'mode':'black'})
        black = self.frame('legacy-black')
        self.assert_check('legacy black remains black after selecting custom mask color', color_fraction(black, self.roi(), (0,0,0), 0) == 1)

        image_path = self.runtime / 'four-colors.png'
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 40, 20)
        paint = cairo.Context(surface)
        for x,y,rgb in [(0,0,(210,40,40)),(20,0,(40,210,40)),(0,10,(40,40,210)),(20,10,(210,180,30))]:
            paint.set_source_rgb(*(c/255 for c in rgb)); paint.rectangle(x,y,20,10); paint.fill()
        paint.set_source_rgb(0,1,1); paint.rectangle(0,0,5,20); paint.fill()
        surface.write_to_png(str(image_path))
        expected = {'stretch':(0,255,255),'fill':(210,40,40),'fit':(21,37,53),'tile':(40,210,40)}
        for fit in expected:
            self.patch({'mode':'image','image_path':str(image_path),'image_fit':fit})
            image = self.frame('image-' + fit)
            pixel = self.point(image,20,40)
            self.assert_check(fit + ': expected crop, letterbox or tile at known synthetic point',
                              all(abs(a-b)<=3 for a,b in zip(pixel,expected[fit])), pixel=pixel, expected=expected[fit])
        self.patch({'image_fit':'fit'})
        fit = self.frame('image-fit-orientation')
        top = self.point(fit, 250, 85)
        bottom = self.point(fit, 250, 155)
        self.assert_check('PNG orientation and aspect ratio are preserved in fit mode',
                          all(abs(a-b)<=3 for a,b in zip(top,(40,210,40))) and
                          all(abs(a-b)<=3 for a,b in zip(bottom,(210,180,30))), top=top, bottom=bottom)

        shader = self.runtime / 'custom.frag'
        shader.write_text('vec4 hyprveil(vec2 uv) { return vec4(.1,.65,.3,0.0); }\n')
        self.patch({'mode':'spoiler','shader_path':str(shader),'eye':False,'speed':100})
        custom = self.frame('custom-glsl-opaque')
        self.assert_check('minimal GLSL compiles without unused built-in uniforms and forces opaque output',
                          color_fraction(custom,self.roi(),(26,166,76),2)>.99 and self.status()['spoiler_status']=='custom-ready',status=self.status())
        animated = self.runtime / 'animated.frag'
        animated.write_text('vec4 hyprveil(vec2 uv) { return vec4(.1 + .8*fract(time*.17),uv.y*.3,.15,1.0); }\n')
        self.patch({'shader_path':str(animated),'variant':'matte','color':'#000000','darkness':100})
        first=self.frame('custom-animated-first'); time.sleep(.2); second=self.frame('custom-animated-second')
        difference = differing_fraction(first,self.roi(),second,self.roi(),2)
        self.assert_check('custom shader animation is independent of built-in variant and tint',
                          difference>.1 and self.last_capture_status['spoiler_animation_armed'],
                          changed_fraction=difference, capture_status=self.last_capture_status)
        invalid = self.runtime / 'broken.frag'; invalid.write_text('not a GLSL function\n')
        self.patch({'shader_path':str(invalid)})
        fallback=self.frame('custom-bad-glsl')
        attempts=self.status()['spoiler_shader_attempts']; self.frame('custom-bad-glsl-repeat')
        self.assert_check('compile errors use opaque chosen fill and do not retry every frame',
                          color_fraction(fallback,self.roi(),(21,37,53),1)>.99 and
                          self.status()['spoiler_status']=='color-fallback' and self.status()['spoiler_shader_attempts']==attempts)
        unsafe=self.runtime/'sampler.frag'; unsafe.write_text('uniform sampler2D original; vec4 hyprveil(vec2 uv){return texture(original,uv);}')
        self.patch({'shader_path':str(unsafe)})
        fallback=self.frame('custom-resource-refused')
        self.assert_check('custom GLSL cannot sample inherited scene textures',color_fraction(fallback,self.roi(),(21,37,53),1)>.99)
        replacement = self.runtime / 'replaced.frag'; replacement.write_text(shader.read_text())
        self.patch({'shader_path':str(replacement)})
        replacement.unlink(); replacement.symlink_to(shader)
        fallback=self.frame('custom-symlink-race')
        self.assert_check('shader path replaced by symlink fails closed at actual read',color_fraction(fallback,self.roi(),(21,37,53),1)>.99)

        icon_path=self.runtime/'custom-icon.png'
        icon=cairo.ImageSurface(cairo.FORMAT_ARGB32,40,20); context=cairo.Context(icon)
        context.set_source_rgba(0,1,1,1); context.paint(); icon.write_to_png(str(icon_path))
        self.patch({'mode':'spoiler','shader_path':'','icon':'custom','icon_path':str(icon_path),'eye':True,
                    'icon_opacity':100,'eye_size':80,'color':'#ffffff','variant':'matte','darkness':100,'speed':0})
        icon_frame=self.frame('custom-png-icon')
        box=self.protected(); centre=(box['size'][0]//2,box['size'][1]//2)
        centre_pixel=self.point(icon_frame,*centre)
        above=self.point(icon_frame,centre[0],centre[1]-30)
        self.assert_check('custom icon preserves aspect and uses existing size and opacity controls',
                          all(abs(a-b)<=3 for a,b in zip(centre_pixel,(0,255,255))) and above==(0,0,0),centre=centre_pixel,above=above)
        self.patch({'icon_opacity':0})
        no_icon=self.frame('custom-icon-opacity-zero')
        self.assert_check('custom icon opacity zero hides only the icon',color_fraction(no_icon,self.roi(),(0,0,0),0)==1)
        self.report['compositor']=json.loads(self.ctl('-j','version'))
        self.report['native_status']=self.status()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent-runtime',required=True)
    parser.add_argument('--parent-display',required=True)
    parser.add_argument('--plugin',type=Path,default=PROJECT/'build/hyprveil.so')
    args=parser.parse_args(); run=RenderAssetsSmoke(args)
    def interrupted(signum,frame): raise KeyboardInterrupt('asset smoke interrupted')
    signal.signal(signal.SIGINT,interrupted); signal.signal(signal.SIGTERM,interrupted)
    try:
        run.start(); run.tests(); run.report['ok']=all(c['ok'] for c in run.checks)
    except BaseException as error:
        run.report['error']=str(error); run.report['ok']=False
    finally:
        run.cleanup(); run.save()
    print(json.dumps({'ok':run.report['ok'],'checks':len(run.checks),'report':str(run.artifacts/'report.json') if run.artifacts else None}),flush=True)
    return 0 if run.report['ok'] else 1
if __name__=='__main__': raise SystemExit(main())
