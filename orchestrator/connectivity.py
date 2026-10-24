"""Static separation of observed objects and actual boundary openings.

Coordinate compression preserves one-pixel gaps without scanning every world pixel.
Connected means only that static geometry does not separate two positions: it does
not promise a jump route, safety, or support from moving platforms.
"""
from bisect import bisect_right
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
import math


@dataclass(frozen=True)
class Grid:
    bounds: tuple
    size: tuple
    xs: tuple
    ys: tuple
    labels: tuple
    count: int

    @property
    def width(self):
        return len(self.xs)-1

    def region(self, x, y):
        x, y = x-self.bounds[0], y-self.bounds[1]
        if not 0 <= x < self.xs[-1] or not 0 <= y < self.ys[-1]:
            return None
        label=self.labels[(bisect_right(self.ys,y)-1)*self.width+bisect_right(self.xs,x)-1]
        return label if label >= 0 else None

    def touching(self, rect, region):
        x,y,w,h=rect; bx,by,_,_=self.bounds; aw,ah=self.size
        for r in range(len(self.ys)-1):
            if self.ys[r+1]+by <= y-ah+1 or self.ys[r]+by >= y+h:continue
            for c in range(self.width):
                if (self.xs[c+1]+bx > x-aw+1 and self.xs[c]+bx < x+w
                        and self.labels[r*self.width+c]==region):return True
        return False

    def boundary(self, side, span):
        bx,by,_,_=self.bounds; aw,ah=self.size
        labels=set(); openings=[]
        vertical=side in {'left','right'}
        n=len(self.ys)-1 if vertical else self.width
        for i in range(n):
            if vertical:
                index=i*self.width+(0 if side=='left' else self.width-1)
                begin,end=self.ys[i]+by+ah/2,self.ys[i+1]+by+ah/2
            else:
                index=i+(0 if side=='top' else (len(self.ys)-2)*self.width)
                begin,end=self.xs[i]+bx+aw/2,self.xs[i+1]+bx+aw/2
            begin,end=max(begin,span[0]),min(end,span[1])
            label=self.labels[index]
            if label<0 or begin>=end:continue
            labels.add(label)
            if openings and openings[-1][1]==begin:openings[-1][1]=end
            else:openings.append([begin,end])
        return labels,openings


@lru_cache(maxsize=24)
def partition(bounds, size, solids):
    bx,by,bw,bh=bounds; aw,ah=size; W,H=bw-aw+1,bh-ah+1
    if W<=0 or H<=0:return None
    rectangles=[];xs={0,W};ys={0,H}
    for x,y,w,h in solids:
        left,right=max(0,math.floor(x-bx-aw)+1),min(W,math.ceil(x+w-bx))
        top,bottom=max(0,math.floor(y-by-ah)+1),min(H,math.ceil(y+h-by))
        if left>=right or top>=bottom:continue
        rectangles.append((left,top,right,bottom));xs.update((left,right));ys.update((top,bottom))
    xs,ys=tuple(sorted(xs)),tuple(sorted(ys));width,height=len(xs)-1,len(ys)-1
    if width*height>250_000:return None  # Unknown, not a false unreachable verdict.
    xi={x:i for i,x in enumerate(xs)};yi={y:i for i,y in enumerate(ys)}
    blocked=bytearray(width*height)
    for left,top,right,bottom in rectangles:
        a,b=xi[left],xi[right]
        for r in range(yi[top],yi[bottom]):blocked[r*width+a:r*width+b]=b'\1'*(b-a)
    labels=[-1 if b else -2 for b in blocked];count=0
    for start,label in enumerate(labels):
        if label!=-2:continue
        labels[start]=count;queue=deque([start])
        while queue:
            i=queue.popleft();r,c=divmod(i,width)
            for neighbor in ((i-1 if c else -1),(i+1 if c<width-1 else -1),
                             (i-width if r else -1),(i+width if r<height-1 else -1)):
                if neighbor>=0 and labels[neighbor]==-2:
                    labels[neighbor]=count;queue.append(neighbor)
        count+=1
    return Grid(bounds,size,xs,ys,tuple(labels),count)


def observed_solids(obs):
    if 'solids' in obs:return tuple(sorted(tuple(row) for row in obs['solids']))
    tiles=obs.get('tiles') or {}
    if tiles.get('scope')!='room':return None
    ox,oy=tiles['origin'];cell=tiles.get('cell',8);rects=[];previous={}
    for r,row in enumerate(tiles.get('rows') or []):
        current={};c=0
        while c<len(row):
            if row[c]!='#':c+=1;continue
            start=c
            while c<len(row) and row[c]=='#':c+=1
            key=(start,c)
            if key in previous:
                i=previous[key];rects[i][3]+=cell
            else:
                i=len(rects);rects.append([ox+start*cell,oy+r*cell,(c-start)*cell,cell])
            current[key]=i
        previous=current
    return tuple(sorted(tuple(r) for r in rects))


def static_connectivity(obs):
    bounds=(obs.get('level') or {}).get('bounds');hitbox=(obs.get('player') or {}).get('hitbox')
    solids=observed_solids(obs)
    if not bounds or not hitbox or solids is None:return None
    x,y,w,h=hitbox;bounds=tuple(bounds)
    point=partition(bounds,(1,1),solids);body=partition(bounds,(w,h),solids)
    if point is None or body is None:return None
    region=point.region(x+w/2,y+h/2);body_region=body.region(x,y)
    if region is None:return None
    result={'basis':'static geometry only','collider':[w,h],'regions':point.count,'exits':[],'entities':[],
            'limitations':'连通不保证动作可达；未验证危险、动态物体或改变姿态后的路线。'}
    for e in obs.get('exits') or []:
        side=e.get('side')
        if side not in {'left','right','top','bottom'}:continue
        span=e.get('y' if side in {'left','right'} else 'x')
        if not isinstance(span,list) or len(span)!=2:continue
        labels,openings=point.boundary(side,span);body_labels,_=body.boundary(side,span)
        relation=('blocked' if not labels else 'separated' if region not in labels else
                  'narrow' if body_region is not None and body_region not in body_labels else 'connected')
        result['exits'].append({**e,'relation':relation,'openings':openings})
    for e in obs.get('entities') or []:
        if e.get('cat') not in {'collectible','spring','refill'}:continue
        rect=tuple(e[k] for k in ('x','y','w','h'))
        relation=('separated' if not point.touching(rect,region) else
                  'narrow' if body_region is not None and not body.touching(rect,body_region) else 'connected')
        result['entities'].append({'id':e.get('id'),'type':e.get('type'),'relation':relation})
    return result
