using System.Runtime.InteropServices;
using System.IO.MemoryMappedFiles;
using CelesteAI.Bridge;
if (!OperatingSystem.IsWindows()) throw new PlatformNotSupportedException();
var game = args.Length > 0 ? args[0] : Environment.GetEnvironmentVariable("CELESTE_DIR") ?? throw new ArgumentException("Pass the installed Celeste directory");
NativeLibrary.SetDllImportResolver(typeof(FMOD.System).Assembly, (name,asm,path)=>NativeLibrary.Load(Path.Combine(game,"everest-lib","lib64-win-x64",name.Contains("studio")?"fmodstudio64.dll":"fmod64.dll")));
void Check(FMOD.RESULT r) { if(r!=FMOD.RESULT.OK)throw new Exception(r.ToString()); }
Check(FMOD.Studio.System.create(out var live)); live.getLowLevelSystem(out var core);
bool asyncSource = Environment.GetEnvironmentVariable("ASYNC_SOURCE") == "1";
Check(core.setOutput(asyncSource ? FMOD.OUTPUTTYPE.NOSOUND : FMOD.OUTPUTTYPE.NOSOUND_NRT)); Check(core.setSoftwareFormat(48000,FMOD.SPEAKERMODE.STEREO,0));Check(core.setDSPBufferSize(800,2));
Check(live.initialize(256,asyncSource ? FMOD.Studio.INITFLAGS.NORMAL : FMOD.Studio.INITFLAGS.SYNCHRONOUS_UPDATE,FMOD.INITFLAGS.NORMAL,IntPtr.Zero));
foreach(var f in new[]{"Master Bank.bank","Master Bank.strings.bank","music.bank","sfx.bank","ui.bank"})Check(live.loadBankFile(Path.Combine(game,"Content","FMOD","Desktop",f),FMOD.Studio.LOAD_BANK_FLAGS.NORMAL,out var bank));
Check(live.getEvent("event:/music/lvl0/intro",out var desc)); Check(desc.createInstance(out var music));Check(music.start());
// Start recording 5 s into the music (past the quiet intro), like a recording started mid-level. WARM overrides the frame count.
int warm=int.Parse(Environment.GetEnvironmentVariable("WARM")??"300"); for(int i=0;i<warm;i++)live.update();
// This one-shot was released before recording hooks were installed. Startup must still retire
// its copy after its independent recording tail ends, without truncating the tail.
// Default landing has no surface sample: it exercises ownership without masking the BGM checks.
Check(live.getEvent("event:/char/madeline/landing",out var seededDesc));
Check(seededDesc.createInstance(out var seededOneShot)); Check(seededOneShot.start()); Check(seededOneShot.release());
RecordingAudio.Load();
try {
RecordingAudio.Start(live,Path.Combine(game,"Content","FMOD","Desktop"),47876);
if (Environment.GetEnvironmentVariable("LIFECYCLE_ONLY") == "1") { SnapshotChecks.Run(live); MissingEventChecks.Run(live, game, music); RestartChecks.Run(live, game, music); LifecycleChecks.Run(live, music); return; }
if (!asyncSource) {
    var startupMixer=typeof(RecordingAudio).GetField("full",System.Reflection.BindingFlags.NonPublic|System.Reflection.BindingFlags.Static)!.GetValue(null)!;
    var startupPairs=(System.Collections.IDictionary)startupMixer.GetType().GetField("Pairs")!.GetValue(startupMixer)!;
    if(!startupPairs.Contains(seededOneShot.getRaw()))throw new Exception("Startup one-shot was not seeded; retirement test would be vacuous");
}
using var map=MemoryMappedFile.OpenExisting(@"Local\CelesteAIRecordAudio-47876"); using var view=map.CreateViewAccessor();
const int Block=6400, Slot=64+2*Block;
if(view.ReadInt32(4)!=2||view.ReadInt32(24)!=Slot||view.ReadInt32(28)!=2)throw new Exception("Unexpected shared memory layout");
float[] ReadBlock(long token,int track){var bytes=new byte[Block];view.ReadArray(64L+token%1024*Slot+64+track*Block,bytes,0,Block);var pcm=new float[1600];Buffer.BlockCopy(bytes,0,pcm,0,Block);return pcm;}
double Energy(float[] pcm)=>pcm.Sum(x=>(double)x*x);
var flags=System.Reflection.BindingFlags.NonPublic|System.Reflection.BindingFlags.Static;
object Mixer(string name)=>typeof(RecordingAudio).GetField(name,flags)!.GetValue(null)!;
FMOD.Studio.EventInstance CopyOf(string mixer,FMOD.Studio.EventInstance live){
    var pairs=(System.Collections.IDictionary)Mixer(mixer).GetType().GetField("Pairs")!.GetValue(Mixer(mixer))!;
    var pair=pairs[live.getRaw()]!; return (FMOD.Studio.EventInstance)pair.GetType().GetProperty("Copy")!.GetValue(pair)!;}
double energy=0, playEnergy=0; int firstAudible=-1;
// Optional second argument exports the continuous PCM (full track) for tools/check_recording_audio.py.
using var capturedPcm = args.Length > 1 ? new MemoryStream() : null;
for(int i=1;i<=120;i++) { live.update(); RecordingAudio.Advance(); RecordingAudio.Publish(i,false);
    var pcm=ReadBlock(i,0); energy+=Energy(pcm); playEnergy+=Energy(ReadBlock(i,1)); if(firstAudible<0&&Energy(pcm)>0)firstAudible=i;
    if(view.ReadInt32(64L+i*Slot+12)!=0)throw new Exception("Real frame marked as replay");
    var bytes=new byte[Block];Buffer.BlockCopy(pcm,0,bytes,0,Block);capturedPcm?.Write(bytes); }
if (capturedPcm != null) {
    using var wav = new BinaryWriter(File.Create(args[1]));
    var pcm = capturedPcm.ToArray();
    wav.Write(System.Text.Encoding.ASCII.GetBytes("RIFF")); wav.Write(36 + pcm.Length);
    wav.Write(System.Text.Encoding.ASCII.GetBytes("WAVEfmt ")); wav.Write(16);
    wav.Write((short)3); wav.Write((short)2); wav.Write(48000); wav.Write(48000 * 8);
    wav.Write((short)8); wav.Write((short)32);
    wav.Write(System.Text.Encoding.ASCII.GetBytes("data")); wav.Write(pcm.Length); wav.Write(pcm);
    Console.WriteLine("PCM WAV: " + args[1]);
}
Console.WriteLine($"ERROR={RecordingAudio.Error ?? "none"} ENERGY full={energy} play={playEnergy} FIRST_AUDIBLE_FRAME={firstAudible}");
foreach(var name in new[]{"full","play"}) {
    var pairs=(System.Collections.IDictionary)Mixer(name).GetType().GetField("Pairs")!.GetValue(Mixer(name))!;
    if(pairs.Contains(seededOneShot.getRaw()))throw new Exception("Pre-released startup one-shot remained tracked after its tail");
}
Console.WriteLine("PASS: pre-released startup one-shot is retired after its recording tail");
// Both mixers render the same real frames from the same state: identical output (streams are driven from update, so no
// wall-clock stream thread can starve one of them). The seeded music needs a few blocks to seek into its stream.
if(energy!=playEnergy)throw new Exception("The two tracks differ on real frames (stream not driven from update?)");
if(warm>=300&&(firstAudible<1||firstAudible>15))throw new Exception($"Seeded music took {firstAudible} frames to start");
var copy=CopyOf("play",music); var fullCopy=CopyOf("full",music);
copy.getTimelinePosition(out var before); fullCopy.getTimelinePosition(out var fullBefore);
for(int i=0;i<120;i++)live.update();
copy.getTimelinePosition(out var after);music.getTimelinePosition(out var livePos);
Console.WriteLine($"INDEPENDENCE before={before} after={after} full={fullBefore} live={livePos}");
if(RecordingAudio.Error!=null||energy<=0||playEnergy<=0||before!=after)throw new Exception("Acceptance failed");
// Planning replay: the full track keeps the music going (SFX buses paused, as in the live game); the play track does not advance.
foreach(var bus in new[]{"bus:/gameplay_sfx","bus:/ui_sfx","bus:/music/stings"}) if(live.getBus(bus,out var b)==FMOD.RESULT.OK)b.setPaused(true);
double replayEnergy=0, replayPlay=0;
for(int i=121;i<=180;i++){ RecordingAudio.AdvanceReplay(); RecordingAudio.Publish(i,true);
    replayEnergy+=Energy(ReadBlock(i,0)); replayPlay+=Energy(ReadBlock(i,1));
    if(view.ReadInt32(64L+i*Slot+12)!=1)throw new Exception("Replay frame not marked"); }
copy.getTimelinePosition(out var afterReplay); fullCopy.getTimelinePosition(out var fullAfterReplay);
var fullStudio=(FMOD.Studio.System)Mixer("full").GetType().GetField("studio",System.Reflection.BindingFlags.NonPublic|System.Reflection.BindingFlags.Instance)!.GetValue(Mixer("full"))!;
bool sfxPaused = fullStudio.getBus("bus:/ui_sfx",out var copyBus)!=FMOD.RESULT.OK || (copyBus.getPaused(out var p)==FMOD.RESULT.OK && p);
Console.WriteLine($"REPLAY full energy={replayEnergy} play energy={replayPlay} full {fullBefore}->{fullAfterReplay} ms, play {after}->{afterReplay} ms, sfx paused={sfxPaused}");
if(replayEnergy<=0||replayPlay!=0||afterReplay!=after||fullAfterReplay-fullBefore<900||!sfxPaused||RecordingAudio.ReplayFrames!=60)throw new Exception("Replay tracks failed");
// Replay -> frozen (master paused, nothing rendered) -> resume: the first real frame must have sound. Bus pauses are
// mirrored as they are issued (the live game's async getters lag one update, which made this frame silent).
string[] sfxBuses={"bus:/gameplay_sfx","bus:/ui_sfx","bus:/music/stings"};
foreach(var bus in sfxBuses) if(live.getBus(bus,out var b)==FMOD.RESULT.OK)b.setPaused(true);
for(int i=200;i<210;i++){RecordingAudio.AdvanceReplay();RecordingAudio.Publish(i,true);}
Check(live.getBus("bus:/",out var masterBus)); Check(masterBus.setPaused(true));
var fullMaster=fullStudio.getBus("bus:/",out var fm)==FMOD.RESULT.OK&&fm.getPaused(out var fmp)==FMOD.RESULT.OK&&fmp;
foreach(var bus in sfxBuses) if(live.getBus(bus,out var b)==FMOD.RESULT.OK)b.setPaused(false);
Check(masterBus.setPaused(false));
RecordingAudio.Advance();RecordingAudio.Publish(210,false);
Console.WriteLine($"SEAM mirrored master pause={fullMaster} first real frame after replay: full={Energy(ReadBlock(210,0))} play={Energy(ReadBlock(210,1))}");
if(!fullMaster||Energy(ReadBlock(210,0))<=0||Energy(ReadBlock(210,1))<=0)throw new Exception("First real frame after a replay and a pause is silent");
// A replay token published without a rendered block must be silence, never a repeated block.
RecordingAudio.Publish(181,true); if(Energy(ReadBlock(181,0))!=0)throw new Exception("Stale block was repeated");
// New one-shot events must be copied even when the game immediately releases them.
Check(live.getEvent("event:/ui/main/button_select",out var uiDesc));
Check(uiDesc.createInstance(out var sound));Check(sound.start());Check(sound.release());
foreach(var m in new[]{"full","play"}){var pairs=(System.Collections.IDictionary)Mixer(m).GetType().GetField("Pairs")!.GetValue(Mixer(m))!;
    if (!pairs.Contains(sound.getRaw()))throw new Exception("Released one-shot was not mirrored in "+m);}
Check(music.setVolume(0.25f));RecordingAudio.Advance();copy.getVolume(out var copiedVolume,out _);
if(Math.Abs(copiedVolume-0.25f)>0.001f)throw new Exception("Volume was not mirrored");
// Stale ring entries must carry the new token, allowing OBS to reject old captures.
RecordingAudio.Publish(1025,false);
if(view.ReadInt64(64+Slot)!=1025)throw new Exception("Ring slot did not commit the new token");
Check(music.stop(FMOD.Studio.STOP_MODE.IMMEDIATE));RecordingAudio.Advance();
copy.getPlaybackState(out var stopped); fullCopy.getPlaybackState(out var fullStopped);
if(stopped!=FMOD.Studio.PLAYBACK_STATE.STOPPED||fullStopped!=FMOD.Studio.PLAYBACK_STATE.STOPPED)throw new Exception("Music stop was not mirrored");
// A stale handle on one mirrored event drops only that mirror; recording continues (10-27, 5A lost ~12 s to one).
{
    Check(live.getEvent("event:/ui/main/button_select",out var staleDesc)); Check(staleDesc.createInstance(out var stale)); Check(stale.start());
    var fullMixer=Mixer("full"); var apply=fullMixer.GetType().GetMethod("Apply")!;
    var droppedField=fullMixer.GetType().GetField("Dropped")!; long droppedBefore=(long)droppedField.GetValue(fullMixer)!;
    apply.Invoke(fullMixer,new object[]{stale,"test setVolume",(Func<FMOD.Studio.EventInstance,FMOD.RESULT>)(c=>FMOD.RESULT.ERR_INVALID_HANDLE)});
    var stalePairs=(System.Collections.IDictionary)fullMixer.GetType().GetField("Pairs")!.GetValue(fullMixer)!;
    if(stalePairs.Contains(stale.getRaw()))throw new Exception("Stale mirror was not dropped");
    if((long)droppedField.GetValue(fullMixer)!!=droppedBefore+1)throw new Exception("Stale handle was not counted");
    RecordingAudio.Advance(); RecordingAudio.Publish(1026,false);
    if(RecordingAudio.Error!=null||view.ReadInt32(64L+(1026%1024)*Slot+8)!=0)throw new Exception("A stale handle failed the recording: "+RecordingAudio.Error);
    bool named=false;
    try{apply.Invoke(fullMixer,new object[]{stale,"test other",(Func<FMOD.Studio.EventInstance,FMOD.RESULT>)(c=>FMOD.RESULT.ERR_INTERNAL)});}
    catch(System.Reflection.TargetInvocationException e) when (e.InnerException is InvalidOperationException inner && inner.Message.Contains("test other")){named=true;}
    if(!named)throw new Exception("Other FMOD errors must still fail, naming the call");
    Check(stale.stop(FMOD.Studio.STOP_MODE.IMMEDIATE)); Check(stale.release()); RecordingAudio.Advance();
    if(RecordingAudio.Error!=null)throw new Exception("Recording failed after the stale-handle checks: "+RecordingAudio.Error);
}
MissingEventChecks.Run(live, game, music);
RestartChecks.Run(live, game, music);
LifecycleChecks.Run(live, music);
Console.WriteLine("PASS: PCM on both tracks, independent timelines, replay music on the full track only, one-shots, volume, stop, ring wrap and stale handles");
} finally {RecordingAudio.Unload();live.release();}
if(RecordingAudio.Error!=null)throw new Exception("Mixer shutdown produced a false frame error: "+RecordingAudio.Error);
namespace CelesteAI.Bridge { static class Log {public static void Info(string s)=>Console.WriteLine(s); public static void Warn(string s)=>Console.WriteLine(s); public static void Error(string s)=>Console.WriteLine(s);} }
