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

// 在真实 EntityList.RenderExcept 上安装同一IL接线，只把绘制替换成顺序日志，不建立GPU。
var trace=new List<string>();
var queue=new RenderDepthQueue<string>();
var scene=(Scene)RuntimeHelpers.GetUninitializedObject(typeof(Scene));
var entityList=new EntityList(scene);
void Add(string name, double depth, bool visible=true, int tag=0) {
    var entity=new TraceEntity(name,trace) {Visible=visible,Tag=tag};
    entity.actualDepth=depth;
    entityList.entities.Add(entity);
}
Add("background",0);
Add("hidden-spinner",-8500,false);
Add("terrain",-10000);
Add("excluded-hud",-11000,true,1);
queue.Add(-8498,"border");queue.Add(-8499,"connector");queue.Add(-8500,"body");queue.Sort();
bool injected=false;
using(var hook=new MonoMod.RuntimeDetour.ILHook(typeof(EntityList).GetMethod(nameof(EntityList.RenderExcept))!,il => {
    injected=EntityRenderDepthHook.Inject(il,
        (entity,list,tags) => { if(ReferenceEquals(list,entityList)) queue.Before(entity.actualDepth,trace.Add); },
        (list,tags) => { if(ReferenceEquals(list,entityList)) queue.Finish(trace.Add); });
})) {
    Check(injected,"Native RenderExcept depth boundary was not found");
    entityList.RenderExcept(1);
    Check(string.Join(",",trace)=="background,border,connector,body,terrain","Proxy depth or native visibility/tag behavior changed: "+string.Join(",",trace));
    Check(entityList.entities.Count==4 && entityList.ToAdd.Count==0 && !entityList.entities[1].Visible,
          "Depth pass mutated native entities or pending queues");
    trace.Clear();queue.Clear();queue.Add(-20000,"last-depth");queue.Sort();
    entityList.RenderExcept(1);
    Check(trace[^1]=="last-depth","Last supplemental depth did not flush inside native render pass");
    trace.Clear();queue.Clear();
    entityList.RenderExcept(1);
    Check(string.Join(",",trace)=="background,terrain","Empty supplemental pass changed normal rendering");
    trace.Clear();queue.Clear();queue.Add(-20000,"empty-list-finish");queue.Sort();
    var unrelated=new EntityList(scene);
    unrelated.RenderExcept(1);
    Check(trace.Count==0,"Unrelated entity list consumed the supplemental pass");
    entityList.entities.Clear();
    entityList.RenderExcept(1);
    Check(string.Join(",",trace)=="empty-list-finish","Empty native list skipped supplemental finish");
}
Console.WriteLine("PASS: native EntityList IL depth insertion preserves visibility, tags, foreground occlusion, end flush and entity ownership");

class TraceEntity(string name,List<string> trace) : Entity {
    public override void Render() => trace.Add(name);
}
