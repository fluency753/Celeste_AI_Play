using Celeste;
using Monocle;
using Microsoft.Xna.Framework;
using System.Runtime.CompilerServices;
using CelesteAI.Bridge;

void Check(bool condition,string message) { if (!condition) throw new Exception(message); }
var atlas=NativeDustFixture.TokenAtlas();
var previousAtlas=GFX.Game;
GFX.Game=atlas;
try {
    int comparisons=0, clippedTracks=0, expandedNodes=0, maskedNodes=0, shiftedGraphics=0;
    void Compare(string label,int seed,Vector2 position,bool ignore=false,bool expand=true,
                 Type? ownerType=null,Action<NativeDustFixture,DustGraphic>? arrange=null) {
        var fixture=new NativeDustFixture();
        Entity? owner=ownerType==null ? null : (Entity)RuntimeHelpers.GetUninitializedObject(ownerType);
        if (owner!=null) owner.Collider=new Circle(6);
        var dust=fixture.MakeDust(position,seed,ignore,expand,owner);
        dust.Position=new Vector2(1.25f,-2.5f);
        fixture.Level.Camera.Position=position-new Vector2(160,90);
        int calls=0;
        dust.OnEstablish=()=>calls++;
        arrange?.Invoke(fixture,dust);
        var oldPosition=dust.Position;
        var oldCollidable=dust.Entity.Collidable;
        var oldEntityCount=fixture.Level.Entities.entities.Count;
        var oldNodes=dust.nodes;
        var expectedRandom=new Random(9182);
        var random=new Random(9182); Calc.Random=random;
        var beforeEntities=fixture.Level.Entities.entities.Select(e=>(e,e.Collidable,e.Position)).ToArray();
        var beforeCounter=DustEdges.DustGraphicEstabledCounter;
        var plan=DustVisualPlan.Build(dust,fixture.Level,7,5);
        Check(ReferenceEquals(Calc.Random,random) && random.Next()==expectedRandom.Next(),label+": proxy changed ambient RNG");
        Check(!dust.Estableshed && ReferenceEquals(oldNodes,dust.nodes) && dust.nodes.Count==0
              && dust.LeftNodes.Count==0 && dust.RightNodes.Count==0 && dust.TopNodes.Count==0 && dust.BottomNodes.Count==0,
              label+": proxy established native nodes");
        Check(dust.Position==oldPosition && dust.Entity.Collidable==oldCollidable && calls==0
              && fixture.Level.Entities.entities.Count==oldEntityCount && DustEdges.DustGraphicEstabledCounter==beforeCounter,
              label+": proxy changed native lifecycle state");
        foreach (var (entity,collidable,at) in beforeEntities)
            Check(entity.Collidable==collidable && entity.Position==at,label+": proxy changed adjacent entity");
        dust.AddDustNodesIfInCamera();
        Check(dust.Estableshed && dust.nodes.Count==4,label+": native no-GPU fixture did not establish");
        int disabled=dust.nodes.Count(n=>!n.Enabled);
        maskedNodes+=disabled;
        expandedNodes+=dust.nodes.Count(n=>n.Angle.LengthSquared()>2);
        if (dust.Position!=oldPosition) shiftedGraphics++;
        if (label.StartsWith("track-wall-")) {
            Check(disabled==2,label+": reference fixture did not exercise actual track clipping");
            clippedTracks++;
        }
        if (label.StartsWith("quadrant-mask-")) {
            int mask=int.Parse(label["quadrant-mask-".Length..]);
            Check(disabled==System.Numerics.BitOperations.PopCount((uint)mask),label+": native fixture did not actually collide with its quadrant solids");
        }
        Check(dust.Position-oldPosition==plan.Offset,label+": establishment offset differs from native");
        for (int i=0;i<4;i++) {
            var actual=dust.nodes[i]; var proxy=plan.Nodes[i];
            Check(ReferenceEquals(actual.Base,atlas.orderedTexturesCache["danger/dustcreature/base"][proxy.Base])
                && ReferenceEquals(actual.Overlay,atlas.orderedTexturesCache["danger/dustcreature/overlay"][proxy.Overlay]),
                label+": random texture choice differs from native at node "+i);
            Check(actual.Rotation==proxy.Rotation && actual.Angle==proxy.Angle && actual.Enabled==proxy.Enabled,
                label+": node geometry/random rotation differs from native at node "+i);
        }
        comparisons++;
    }
    foreach (int seed in new[]{0,1,42,int.MaxValue,-16919}) {
        foreach (int mask in Enumerable.Range(0,16))
            Compare("quadrant-mask-"+mask,seed,new Vector2(100,100),arrange:(f,d)=> {
                for (int i=0;i<4;i++) if ((mask & (1<<i))!=0)
                    f.Solid(new Vector2(100+(i%2==0?-8:0),100+(i<2?-8:0)),8,8);
            });
        Compare("self-exclusion",seed,new Vector2(100,100),ownerType:typeof(DustStaticSpinner),arrange:(f,d)=>d.Entity.Collider=new Circle(24));
        Compare("adjacent-static",seed,new Vector2(100,100),ownerType:typeof(DustStaticSpinner),arrange:(f,d)=> {
            f.Spinner(new Vector2(116,100)); f.Spinner(new Vector2(100,116));
        });
        Compare("disabled-neighbour-and-owner",seed,new Vector2(100,100),ownerType:typeof(DustStaticSpinner),arrange:(f,d)=> {
            d.Entity.Collidable=false; f.Spinner(new Vector2(116,100),false);
        });
        Compare("negative-fractional",seed,new Vector2(-100.75f,-20.25f),arrange:(f,d)=> {
            f.Solid(new Vector2(-120,-40),18,18); f.Spinner(new Vector2(-84.75f,-20.25f));
        });
        Compare("no-expansion",seed,new Vector2(100,100),expand:false,arrange:(f,d)=>f.Spinner(new Vector2(116,100)));
        Compare("rotating-ignore-walls",seed,new Vector2(100,100),ignore:true,expand:false,
                ownerType:typeof(DustRotateSpinner),arrange:(f,d)=>f.Solid(new Vector2(80,80),40,40));
        foreach (var direction in new[]{new Vector2(32,0),new Vector2(0,32),new Vector2(-32,0),new Vector2(24,24)}) {
            Compare("track-wall-"+direction,seed,new Vector2(100,100),ignore:true,expand:false,
                    ownerType:typeof(DustTrackSpinner),arrange:(f,d)=>f.Track(d,direction,continuous:true));
            Compare("track-gap-"+direction,seed,new Vector2(100,100),ignore:true,expand:false,
                    ownerType:typeof(DustTrackSpinner),arrange:(f,d)=>f.Track(d,direction,continuous:false));
        }
    }
    Check(clippedTracks>0 && expandedNodes>0 && maskedNodes>0 && shiftedGraphics>0,"Native fixtures did not exercise geometry-dependent establishment");
    Console.WriteLine($"PASS: {comparisons} proxy plans equal actual native establishment (texture tokens, rotations, enabled masks, expansion, offsets and track clipping)");
    Console.WriteLine($"PASS: native fixtures actually exercised {clippedTracks} clipped tracks, {expandedNodes} expanded nodes, {maskedNodes} disabled nodes and {shiftedGraphics} shifted graphics");
    Console.WriteLine("PASS: proxy leaves native nodes, lifecycle, owner/neighbours, scene and ambient random state unchanged");
} finally { GFX.Game=previousAtlas; }

internal sealed class NativeDustFixture {
    internal readonly Level Level;
    internal NativeDustFixture() {
        Level=(Level)RuntimeHelpers.GetUninitializedObject(typeof(Level));
        Level.Entities=new EntityList(Level);
        Level.Tracker=(Tracker)RuntimeHelpers.GetUninitializedObject(typeof(Tracker));
        Level.Tracker.Entities=new() { [typeof(Solid)]=new(), [typeof(DustStaticSpinner)]=new() };
        Level.Camera=new Camera(320,180);
    }
    internal static Atlas TokenAtlas() {
        var atlas=(Atlas)RuntimeHelpers.GetUninitializedObject(typeof(Atlas));
        atlas.orderedTexturesCache=new();
        foreach (var (path,count) in new[]{("base",7),("overlay",5),("center",3)})
            atlas.orderedTexturesCache["danger/dustcreature/"+path]=Enumerable.Range(0,count)
                .Select(_=>(MTexture)RuntimeHelpers.GetUninitializedObject(typeof(MTexture))).ToList();
        return atlas;
    }
    internal void Add(Entity entity) {
        entity.Scene=Level;
        Level.Entities.entities.Add(entity);
        if (entity is Solid) Level.Tracker.Entities[typeof(Solid)].Add(entity);
        if (entity is DustStaticSpinner) Level.Tracker.Entities[typeof(DustStaticSpinner)].Add(entity);
    }
    internal void Solid(Vector2 position,int width,int height) {
        var solid=(Solid)RuntimeHelpers.GetUninitializedObject(typeof(Solid));
        solid.Position=position; solid.Collidable=true; solid.Collider=new Hitbox(width,height);
        Add(solid);
    }
    internal void Spinner(Vector2 position,bool collidable=true) {
        var spinner=(DustStaticSpinner)RuntimeHelpers.GetUninitializedObject(typeof(DustStaticSpinner));
        spinner.Position=position; spinner.Collidable=collidable; spinner.Collider=new Circle(6);
        Add(spinner);
    }
    internal void Track(DustGraphic dust,Vector2 direction,bool continuous) {
        var track=(DustTrackSpinner)dust.Entity;
        track.Start=track.Position; track.End=track.Position+direction;
        track.dusty=dust;
        dust.OnEstablish=track.Establish;
        var along=direction.SafeNormalize(); var normal=new Vector2(-along.Y,along.X);
        for (int step=0;step<direction.Length();step+=8) {
            if (!continuous && step==16) continue;
            var at=track.Position+normal*4+along*step;
            Solid(new Vector2((int)at.X-2,(int)at.Y-2),4,4);
        }
    }
    internal DustGraphic MakeDust(Vector2 position,int seed,bool ignoreSolids=false,bool expand=true,Entity? owner=null) {
        owner??=new Entity(position); owner.Position=position; owner.Collidable=true;
        Add(owner);
        var dust=(DustGraphic)RuntimeHelpers.GetUninitializedObject(typeof(DustGraphic));
        dust.Entity=owner; dust.Visible=true; dust.Active=true; dust.Scale=1;
        dust.nodes=new(); dust.LeftNodes=new(); dust.RightNodes=new(); dust.TopNodes=new(); dust.BottomNodes=new();
        dust.randomSeed=seed; dust.ignoreSolids=ignoreSolids; dust.autoExpandDust=expand;
        dust.eyesExist=false;
        DustEdges.DustGraphicEstabledCounter=0;
        return dust;
    }
}
