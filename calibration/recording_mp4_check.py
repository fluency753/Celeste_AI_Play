"""Offline MP4 integration: actual plugin finish code and real FFmpeg, no OBS/game.

Run from the repository root: python -m calibration.recording_mp4_check
Requires the same ziglang package and FFmpeg used by build_obs_plugin.py.
"""
from datetime import datetime
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys

from orchestrator.purecut import make_pure
from orchestrator.recording import find_ffmpeg, load_config


def check(output: Path):
    output.mkdir(parents=True,exist_ok=False)
    ffmpeg = Path(find_ffmpeg(load_config().get('ffmpeg')))
    probe = ffmpeg.with_name('ffprobe.exe')
    log = output/'commands.log'
    def run(args):
        with log.open('ab') as stream:
            stream.write(('\n'+repr([str(a) for a in args])+'\n').encode('utf-8'))
            subprocess.run([str(a) for a in args],stdin=subprocess.DEVNULL,
                           stdout=stream,stderr=stream,check=True)

    # Compile the actual finish_thread and spawn functions with only OBS state
    # notification/allocation boundaries stubbed. The mux command is not copied
    # into a Python imitation, and CreateProcess/FFmpeg really run.
    source = Path('obs-plugin/celeste-lockstep.c').read_text(encoding='utf-8')
    spawn = source[source.index('static bool spawn('):source.index('/* 开始失败时清理')]
    finish = source[source.index('struct finish_job {'):source.index('static void gp_stop(')]
    harness = output/'plugin_finish_check.c'
    harness.write_text(r'''
#include <windows.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define bstrdup _strdup
#define bfree free
#define PLUGIN "mp4-check"
#define LOG_INFO 0
#define blog(...) ((void)0)
enum rec_state { REC_IDLE, REC_RECORDING, REC_FINISHING, REC_FINISHED, REC_FAILED };
static enum rec_state last_state;
static char last_error[512];
static void rec_set(enum rec_state state, long long frames, const char *error) {
    last_state = state;
    snprintf(last_error,sizeof(last_error),"%s",error ? error : "");
}
static void send_status(void) {}
''' + spawn + finish + r'''
int main(int argc, char **argv) {
    if (argc != 3) return 2;
    struct finish_job *job = calloc(1,sizeof(*job));
    snprintf(job->ffmpeg,sizeof(job->ffmpeg),"%s",argv[1]);
    snprintf(job->dir,sizeof(job->dir),"%s",argv[2]);
    job->written = 120;
    finish_thread(job);
    if (last_state != REC_FINISHED) { fprintf(stderr,"%s\n",last_error); return 1; }
    return 0;
}
''',encoding='utf-8',newline='\n')
    executable = output/'plugin_finish_check.exe'
    run([sys.executable,'-m','ziglang','cc','-target','x86_64-windows-gnu','-O2',harness,'-o',executable])

    raw = output/'raw recording'
    raw.mkdir()
    run([ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc2=size=320x180:rate=60',
         '-frames:v','120','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',raw/'video.mkv'])
    run([ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i','sine=frequency=440:sample_rate=48000',
         '-t','2','-ac','2','-c:a','pcm_f32le',raw/'audio.wav'])
    run([executable,ffmpeg,raw])

    def frame_csv(directory,values):
        (directory/'frames.csv').write_text('index,chapter_ticks\n'+''.join(
            f'{i},{value}\n' for i,value in enumerate(values)),encoding='utf-8',newline='\n')

    def validate(path,frames):
        info = json.loads(subprocess.check_output([str(probe),'-v','error','-count_packets',
            '-show_format','-show_streams','-of','json',str(path)]))
        assert 'mp4' in info['format']['format_name'].split(','), info
        video = next(s for s in info['streams'] if s['codec_type']=='video')
        audio = next(s for s in info['streams'] if s['codec_type']=='audio')
        assert video['codec_name']=='h264' and int(video['nb_read_packets'])==frames,video
        assert audio['codec_name']=='aac' and audio['sample_rate']=='48000' and audio['channels']==2,audio
        assert abs(float(info['format']['duration'])-frames/60)<2/60,info['format']
        atoms = []
        with path.open('rb') as stream:
            while header := stream.read(8):
                size,kind = struct.unpack('>I4s',header)
                atoms.append(kind)
                if size==1:
                    size = struct.unpack('>Q',stream.read(8))[0]
                    stream.seek(size-16,1)
                elif size==0:
                    break
                else:
                    stream.seek(size-8,1)
        assert atoms.index(b'moov') < atoms.index(b'mdat'),atoms
        run([ffmpeg,'-hide_banner','-loglevel','error','-xerror','-i',path,'-f','null','-'])
        return {'file':str(path),'frames':frames,'format':'mp4','codecs':'H.264/AAC','faststart':True,'decode':'passed'}

    results = [validate(raw/'gameplay.mp4',120)]
    frame_csv(raw,range(120))
    same = make_pure(raw,str(ffmpeg),'-c:v libx264 -preset ultrafast')
    assert same['state']=='finished' and same['same_as_full'],same
    results.append(validate(raw/'gameplay_pure.mp4',120))

    legacy = output/'legacy recording'
    legacy.mkdir()
    run([ffmpeg,'-hide_banner','-loglevel','error','-i',raw/'gameplay.mp4','-c','copy',legacy/'gameplay.mkv'])
    frame_csv(legacy,range(120))
    remuxed = make_pure(legacy,str(ffmpeg),'-c:v libx264 -preset ultrafast')
    assert remuxed['state']=='finished' and (legacy/'gameplay.mkv').is_file(),remuxed
    results.append(validate(legacy/'gameplay_pure.mp4',120))

    filtered = output/'filtered recording'
    filtered.mkdir()
    for name in ('video.mkv','audio.wav'):
        shutil.copy2(raw/name,filtered/name)
    frame_csv(filtered,[i if i<40 or i>=80 else -1 for i in range(120)])
    run([ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i','sine=frequency=440:sample_rate=48000',
         '-t',str(80/60),'-ac','2','-c:a','pcm_f32le',filtered/'audio_play.wav'])
    pure = make_pure(filtered,str(ffmpeg),'-c:v libx264 -preset ultrafast')
    assert pure['state']=='finished' and pure['frames']==80 and pure['audio']=='audio_play.wav',pure
    results.append(validate(filtered/'gameplay_pure.mp4',80))
    fallback = output/'fallback recording'
    fallback.mkdir()
    for name in ('video.mkv','audio.wav','frames.csv'):
        shutil.copy2(filtered/name,fallback/name)
    pure = make_pure(fallback,str(ffmpeg),'-c:v libx264 -preset ultrafast')
    assert pure['state']=='finished' and pure['audio']=='audio.wav 裁剪',pure
    results.append(validate(fallback/'gameplay_pure.mp4',80))
    summary = {'results':results,'plugin_finish_thread':'executed production source',
               'external_services':'not connected'}
    (output/'result.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':
    check(Path('local')/('mp4-output-check-'+datetime.now().strftime('%Y%m%d-%H%M%S')))
