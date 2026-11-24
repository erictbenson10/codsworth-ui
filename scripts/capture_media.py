#!/usr/bin/env python3
"""Record the existing local glyph demos with genuine browser control events.

Requires Playwright Chromium and ffmpeg. Serves this checkout only on loopback;
all non-loopback browser requests are blocked. No model or audio is connected.
"""
from __future__ import annotations

import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[1]
MEDIA=ROOT/'media'
QA=ROOT/'.qa'/'media-capture'


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self,*args):
        pass


async def capture(browser, base, route, name, viewport, interaction):
    context=await browser.new_context(viewport=viewport,device_scale_factor=1,
        record_video_dir=str(QA/'raw-video'),record_video_size=viewport)
    async def local_only(request):
        if request.request.url.startswith(base+'/'):
            await request.continue_()
        else:
            await request.abort()
    await context.route('**/*',local_only)
    page=await context.new_page()
    errors=[]
    page.on('pageerror',lambda error: errors.append(str(error)))
    video_start=time.perf_counter()
    await page.goto(base+route,wait_until='networkidle')
    await page.evaluate('document.fonts.ready')
    await page.wait_for_timeout(500)
    start=time.perf_counter()-video_start
    details=await interaction(page)
    duration=time.perf_counter()-video_start-start
    recording=page.video
    await context.close()
    source=await recording.path()
    executable=shutil.which('ffmpeg')
    if not executable:
        raise RuntimeError('ffmpeg is required')
    subprocess.run([executable,'-y','-hide_banner','-loglevel','error','-ss',f'{start:.3f}',
        '-i',source,'-t',f'{duration:.3f}','-an','-vf','fps=25,format=yuv420p','-c:v','libx264',
        '-preset','slow','-crf','24','-movflags','+faststart',str(MEDIA/f'{name}-demo.mp4')],check=True)
    return dict(page=route,video=f'{name}-demo.mp4',viewport=viewport,trim_start=start,
                duration=duration,browser_errors=errors,interaction=details)


async def expressions(page):
    await page.locator('#expressions [data-expression="pleased"]').click()
    await page.wait_for_timeout(650)
    await page.screenshot(path=str(MEDIA/'expressions.png'),full_page=True)
    await page.wait_for_timeout(650)
    await page.locator('#activities [data-activity="thinking"]').click()
    await page.wait_for_timeout(1500)
    await page.locator('#expressions [data-expression="skeptical"]').click()
    await page.wait_for_timeout(1200)
    await page.locator('#activities [data-activity="listening"]').click()
    await page.wait_for_timeout(1200)
    await page.locator('#next').click()
    await page.wait_for_timeout(1000)
    await page.locator('#expressions [data-expression="annoyed"]').click()
    await page.locator('#activities [data-activity="talking"]').click()
    await page.wait_for_timeout(1800)
    await page.locator('#expressions [data-expression="pleased"]').click()
    await page.locator('#activities [data-activity="idle"]').click()
    await page.wait_for_timeout(1500)
    return 'Clicked existing expression/activity buttons and carousel navigation. Talking is a manually selected animation state; no audio.'


async def conversation(page):
    await page.wait_for_timeout(600)
    await page.locator('#mood').select_option('pleased')
    await page.locator('#text').click()
    await page.wait_for_timeout(1400)
    await page.screenshot(path=str(MEDIA/'conversation.png'),full_page=True)
    await page.locator('#finish').click()
    await page.wait_for_timeout(700)
    await page.locator('#mood').select_option('skeptical')
    await page.locator('#text').click()
    await page.wait_for_timeout(1600)
    await page.locator('#interrupt').click()
    await page.wait_for_timeout(800)
    await page.locator('#mood').select_option('concerned')
    await page.locator('#text').click()
    await page.wait_for_timeout(1800)
    await page.locator('#finish').click()
    await page.wait_for_timeout(1400)
    return 'Selected reply moods; clicked Try text, Finish text and Interrupt. All replies are the page\'s existing scripted examples. No audio or model.'


async def main():
    MEDIA.mkdir(exist_ok=True)
    QA.mkdir(parents=True,exist_ok=True)
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(QuietHandler,directory=str(ROOT)))
    worker=threading.Thread(target=server.serve_forever,daemon=True)
    worker.start()
    base=f'http://127.0.0.1:{server.server_port}'
    try:
        async with async_playwright() as playwright:
            browser=await playwright.chromium.launch(headless=True)
            results=[]
            results.append(await capture(browser,base,'/glyphs/preview.html','expressions',
                {'width':1440,'height':1080},expressions))
            results.append(await capture(browser,base,'/glyphs/conversation.html','conversation',
                {'width':1440,'height':900},conversation))
            context=await browser.new_context(viewport={'width':390,'height':844},device_scale_factor=2,
                is_mobile=True,has_touch=True)
            await context.route('**/*',lambda route: route.continue_() if route.request.url.startswith(base+'/') else route.abort())
            page=await context.new_page()
            await page.goto(base+'/glyphs/conversation.html',wait_until='networkidle')
            await page.locator('#text').click()
            await page.wait_for_timeout(1000)
            await page.screenshot(path=str(MEDIA/'conversation-mobile.png'),full_page=True)
            results.append({'page':'/glyphs/conversation.html','mobile_overflow':await page.evaluate('document.documentElement.scrollWidth > innerWidth')})
            await context.close()
            await browser.close()
            (QA/'browser-capture.json').write_text(json.dumps(results,indent=2)+'\n')
            print(json.dumps(results,indent=2))
    finally:
        server.shutdown()
        server.server_close()


if __name__=='__main__':
    asyncio.run(main())
