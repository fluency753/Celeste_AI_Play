using System.Collections;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text.Json;
using Celeste;
using CelesteAI.Bridge;
using Microsoft.Xna.Framework;
using Monocle;

object Field(string name) => typeof(Playback).GetField(name,BindingFlags.Static|BindingFlags.NonPublic)!.GetValue(null)!;
int Count(string name) => ((ICollection)Field(name)).Count;
void Check(bool condition,string message) { if(!condition) throw new Exception(message); }
var level=(Level)RuntimeHelpers.GetUninitializedObject(typeof(Level));
level.Camera=new Camera(320,180);
using var request=JsonDocument.Parse("""{"frames":30,"speed":2,"fade":8,"tracks":[{"p":[0,10,80,10,30,80],"k":3}]}""");
Check(Playback.Start(1,request.RootElement,level)==null,"Start failed");
// 此检查不创建GPU或载入地图；只将渲染游标设到轮末，执行真实的完成/保留/交接逻辑。
typeof(Playback).GetField("frame",BindingFlags.Static|BindingFlags.NonPublic)!.SetValue(null,30);
Check(Playback.Done,"Actual playback did not reach completion");
Playback.Finish();
Check(!Playback.Running && Count("Tracks")==1 && Count("History")==0,"Round completion discarded the colored latest track");
int frozenFrame=(int)Field("frame");
for(int i=0;i<100;i++) Playback.Step(level);
Check((int)Field("frame")==frozenFrame && Count("Tracks")==1,"Idle advanced the visual timeline");
using var empty=JsonDocument.Parse("""{"speed":9,"tracks":[]}""");
Check(Playback.Start(2,empty.RootElement,level)!=null,"Empty round accepted");
Check((float)Field("speed")==2 && Count("Tracks")==1 && Count("History")==0,"Rejected round changed retained visuals");
Check(Playback.Start(3,request.RootElement,level)==null && Count("Tracks")==1 && Count("History")==1,"Next round did not promote previous tracks");
Playback.Reset();
Check(Count("Tracks")==0 && Count("History")==0,"Reset retained route resources");
Check(Playback.Start(4,request.RootElement,level)==null,"Restart failed");
typeof(Playback).GetField("frame",BindingFlags.Static|BindingFlags.NonPublic)!.SetValue(null,30);
Playback.Finish();
Playback.EndOverlay();
Check(Count("Tracks")==0 && Count("History")==0 && !(bool)Field("overlay"),"Real execution did not clear held routes");
Console.WriteLine("PASS: real playback lifecycle retains completed color, freezes idle, rejects empty rounds without mutation, promotes on new drawing, clears on real execution/reset");
