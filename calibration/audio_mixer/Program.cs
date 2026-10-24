using System.Runtime.InteropServices;
using System.IO.MemoryMappedFiles;
using CelesteAI.Bridge;
if (!OperatingSystem.IsWindows()) throw new PlatformNotSupportedException();
var game = args.Length > 0 ? args[0] : Environment.GetEnvironmentVariable("CELESTE_DIR") ?? throw new ArgumentException("Pass the installed Celeste directory");
NativeLibrary.SetDllImportResolver(typeof(FMOD.System).Assembly, (name,asm,path)=>NativeLibrary.Load(Path.Combine(game,"everest-lib","lib64-win-x64",name.Contains("studio")?"fmodstudio64.dll":"fmod64.dll")));
void Check(FMOD.RESULT r) { if(r!=FMOD.RESULT.OK)throw new Exception(r.ToString()); }
Check(FMOD.Studio.System.create(out var live)); live.getLowLevelSystem(out var core);
Check(core.setOutput(FMOD.OUTPUTTYPE.NOSOUND_NRT)); Check(core.setSoftwareFormat(48000,FMOD.SPEAKERMODE.STEREO,0));Check(core.setDSPBufferSize(800,2));
Check(live.initialize(256,FMOD.Studio.INITFLAGS.SYNCHRONOUS_UPDATE,FMOD.INITFLAGS.NORMAL,IntPtr.Zero));
foreach(var f in new[]{"Master Bank.bank","Master Bank.strings.bank","music.bank","ui.bank"})Check(live.loadBankFile(Path.Combine(game,"Content","FMOD","Desktop",f),FMOD.Studio.LOAD_BANK_FLAGS.NORMAL,out var bank));
Check(live.getEvent("event:/music/lvl0/intro",out var desc)); Check(desc.createInstance(out var music));Check(music.start()); for(int i=0;i<30;i++)live.update();
RecordingAudio.Load();
try {
RecordingAudio.Start(live,Path.Combine(game,"Content","FMOD","Desktop"),47876);
using var map=MemoryMappedFile.OpenExisting(@"Local\CelesteAIRecordAudio-47876"); using var view=map.CreateViewAccessor();
double energy=0;
for(int i=1;i<=120;i++) { live.update(); RecordingAudio.Advance(); RecordingAudio.Publish(i,false); var bytes=new byte[6400]; view.ReadArray(64L+i*6464+64,bytes,0,6400);var pcm=new float[1600];Buffer.BlockCopy(bytes,0,pcm,0,6400);energy+=pcm.Sum(x=>(double)x*x); }
Console.WriteLine($"ERROR={RecordingAudio.Error ?? "none"} ENERGY={energy}");
var pairs=(System.Collections.IDictionary)typeof(RecordingAudio).GetField("Pairs",System.Reflection.BindingFlags.NonPublic|System.Reflection.BindingFlags.Static)!.GetValue(null)!;
var pair=pairs[music.getRaw()]!; var copy=(FMOD.Studio.EventInstance)pair.GetType().GetProperty("Copy")!.GetValue(pair)!;
copy.getTimelinePosition(out var before);
for(int i=0;i<120;i++)live.update();
copy.getTimelinePosition(out var after);music.getTimelinePosition(out var livePos);
Console.WriteLine($"INDEPENDENCE before={before} after={after} live={livePos}");
RecordingAudio.Publish(121,true);var silence=new byte[6400];view.ReadArray(64L+121*6464+64,silence,0,6400);Console.WriteLine($"REPLAY SILENT={silence.All(x=>x==0)}");
if(RecordingAudio.Error!=null||energy<=0||before!=after||silence.Any(x=>x!=0))throw new Exception("Acceptance failed");
// New one-shot events must be copied even when the game immediately releases them.
Check(live.getEvent("event:/ui/main/button_select",out var uiDesc));
Check(uiDesc.createInstance(out var sound));Check(sound.start());Check(sound.release());
if (!pairs.Contains(sound.getRaw()))throw new Exception("Released one-shot was not mirrored");
Check(music.setVolume(0.25f));RecordingAudio.Advance();copy.getVolume(out var copiedVolume,out _);
if(Math.Abs(copiedVolume-0.25f)>0.001f)throw new Exception("Volume was not mirrored");
// Stale ring entries must carry the new token, allowing OBS to reject old captures.
RecordingAudio.Publish(1025,false);
if(view.ReadInt64(64+6464)!=1025)throw new Exception("Ring slot did not commit the new token");
Check(music.stop(FMOD.Studio.STOP_MODE.IMMEDIATE));RecordingAudio.Advance();
copy.getPlaybackState(out var stopped);
if(stopped!=FMOD.Studio.PLAYBACK_STATE.STOPPED)throw new Exception("Music stop was not mirrored");
Console.WriteLine("PASS: PCM, independent timelines, replay silence, one-shots, volume, stop and ring wrap");
} finally {RecordingAudio.Unload();live.release();}
if(RecordingAudio.Error!=null)throw new Exception("Mixer shutdown produced a false frame error: "+RecordingAudio.Error);
namespace CelesteAI.Bridge { static class Log {public static void Info(string s)=>Console.WriteLine(s); public static void Error(string s)=>Console.WriteLine(s);} }
