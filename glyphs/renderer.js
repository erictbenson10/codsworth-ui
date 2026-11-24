/* Native pixel geometry shared with codsworth.py; interpolated poses stay articulated. */
(function (root) {
  'use strict';
  const W = 96, H = 112, CX = 48, CY = 35, R = 21;
  const [BG, OUTLINE, DARK, SHADOW, MID, SHELL, LIGHT, HI, GLASS, LENS, GLINT, FLAME, CORE, RUST, EMBER, SEAM] = Array.from({length:16}, (_, i) => i);
  const PALETTE = [[18,20,24],[30,32,31],[48,50,45],[79,83,74],[119,123,108],[162,165,145],[202,204,182],[235,232,206],[16,24,27],[43,61,65],[218,231,227],[245,149,57],[255,228,147],[114,78,49],[195,76,30],[57,63,57]];
  const tau = Math.PI * 2;
  const round = x => { const f = Math.floor(x), d = x - f; return d === .5 ? (f % 2 === 0 ? f : f + 1) : Math.round(x); };
  const clamp = (v, low, high) => Math.max(low, Math.min(high, v));
  class Grid {
    constructor() { this.g = new Uint8Array(W * H); }
    put(r, c, m) { r = round(r); c = round(c); if (r >= 0 && r < H && c >= 0 && c < W) this.g[r * W + c] = m; }
    rect(r0, r1, c0, c1, m) { for (let r=round(r0); r<=round(r1); r++) for(let c=round(c0); c<=round(c1); c++) this.put(r,c,m); }
    ellipse(r,c,ry,rx,m) { for(let y=Math.floor(r-ry);y<=Math.ceil(r+ry);y++) for(let x=Math.floor(c-rx);x<=Math.ceil(c+rx);x++) if(((y-r)/ry)**2+((x-c)/rx)**2<=1) this.put(y,x,m); }
    disc(r,c,radius,m) { this.ellipse(r,c,radius,radius,m); }
    seg(a,b,width,m) {
      const dy=b[0]-a[0],dx=b[1]-a[1],length=dy*dy+dx*dx || 1,margin=width/2;
      for(let r=Math.floor(Math.min(a[0],b[0])-margin);r<=Math.ceil(Math.max(a[0],b[0])+margin);r++) for(let c=Math.floor(Math.min(a[1],b[1])-margin);c<=Math.ceil(Math.max(a[1],b[1])+margin);c++) {
        const t=clamp(((r-a[0])*dy+(c-a[1])*dx)/length,0,1);
        if((r-a[0]-t*dy)**2+(c-a[1]-t*dx)**2<=margin**2) this.put(r,c,m);
      }
    }
    joint([r,c],radius=3) { this.disc(r,c,radius+1,OUTLINE);this.disc(r,c,radius,MID);this.disc(r,c,radius-1,DARK);this.put(r-1,c-1,LIGHT);this.put(r,c,SHELL); }
    strut(a,b,width=4) { this.seg(a,b,width+2,OUTLINE);this.seg(a,b,width,SHADOW);this.seg([a[0],a[1]-1],[b[0],b[1]-1],1,LIGHT); }
    ball(cr,cc) {
      for(let r=Math.floor(cr-R);r<=Math.ceil(cr+R);r++) for(let c=cc-R;c<=cc+R;c++) {
        const x=(c-cc)/R,y=(r-cr)/R,d=x*x+y*y;
        if(d>1)continue;
        let m=d>.91?(x+y<0?MID:OUTLINE):y>.25?(d<.77?DARK:SHADOW):y>-.08?(x>.12?SHADOW:MID):(x<.45?SHELL:MID);
        if(d<.87&&((x+.48)/.18)**2+((y+.1)/.48)**2<1)m=y<.06?HI:LIGHT;
        if(d<.87&&((x-.1)/.14)**2+((y+.04)/.3)**2<1)m=LIGHT;
        if(d<.87&&y<-.58)m=x<.25?LIGHT:SHELL;
        this.put(r,c,m);
      }
      for(let x=-17;x<=17;x++){const y=-12+round(2*(x/17)**2);this.put(cr+y,cc+x,SEAM);this.put(cr+y-1,cc+x,LIGHT);}
      this.rect(cr-19,cr-18,cc-5,cc+5,MID);this.rect(cr-20,cr-20,cc-3,cc+4,LIGHT);
      for(const [y,width] of [[11,10],[14,8],[17,5]]) {this.rect(cr+y,cr+y,cc-Math.floor(width/2)+3,cc+Math.floor(width/2)+3,OUTLINE);this.rect(cr+y+1,cr+y+1,cc-Math.floor(width/2)+3,cc+Math.floor(width/2)+2,MID);}
      this.rect(cr+19,cr+19,cc-5,cc+4,RUST);
    }
    optic(r,c,radius,aperture=.4,glare=0,side=0) {
      r=round(r);c=round(c);
      this.ellipse(r+2,c+1,radius+1,radius+1,OUTLINE);this.disc(r,c,radius,MID);this.disc(r-1,c-1,radius-1,LIGHT);this.disc(r,c,radius-2,DARK);this.disc(r,c,radius-3,MID);
      const inner=radius-3,stop=1.05+clamp(aperture,0,1)*3.45,angle=.25*(1-aperture);
      const normals=Array.from({length:6},(_,i)=>[Math.cos(angle+i*tau/6),Math.sin(angle+i*tau/6)]);
      for(let dy=-inner;dy<=inner;dy++)for(let dx=-inner;dx<=inner;dx++) {
        const distance=Math.hypot(dx,dy);if(distance>inner-.25)continue;
        const opening=Math.max(...normals.map(([nx,ny])=>dx*nx+dy*ny))<=stop;
        let m=GLASS;
        if(!opening){const theta=((Math.atan2(dy,dx)-angle-.14*distance)%tau+tau)%tau,sector=theta/(tau/6);m=glare>=.5?(Math.floor(sector)%2?DARK:SHADOW):(Math.floor(sector)%2?SHADOW:MID);if(sector%1<.12)m=glare>=.5?MID:SHELL;}
        this.put(r+dy,c+dx,m);
      }
      if(glare>0)for(let dy=-inner;dy<=inner;dy++)for(let dx=-inner;dx<=inner;dx++) {
        const boundary=-inner+(inner-1)*glare-side*glare*dx*.45;
        if(dx*dx+dy*dy<(inner-.25)**2&&dy<boundary)this.put(r+dy,c+dx,dy<boundary-1?SHADOW:MID);
      }
      this.put(r-3,c-3,GLINT);this.put(r+3,c+3,LENS);
      for(const [dy,dx] of [[-7,0],[0,-7],[7,0],[0,7]])this.put(r+dy,c+dx,SHADOW);
      this.put(r-radius,c-1,HI);
    }
  }
  function render(pose={}) {
    const {bob=0,arms=[0,0,0],lp=0,rp=0,turn=0,apertures=[.4,.4],gaze=0,reach=0,lift=0,grip=0,tilt=0}=pose;
    const flame=round(pose.flame||0),voice=round(pose.voice||0),glare=clamp(Number(pose.glare||0),0,1);
    const g=new Grid(),cr=CY+bob,[la,ra,rear]=arms,p=(y,x)=>[cr+y,CX+x];
    const rearSh=p(23,-9),rearElbow=p(28+rear,-26),nozzle=p(19+rear,-35);
    g.strut(rearSh,rearElbow,3);g.strut(rearElbow,nozzle,2);g.joint(rearElbow,2);
    g.seg(nozzle,p(19+rear,-41),4,DARK);g.seg(p(18+rear,-36),p(18+rear,-42),1,LIGHT);
    g.rect(cr+18+rear,cr+21+rear,CX-43,CX-41,MID);g.rect(cr+19+rear,cr+20+rear,CX-45,CX-44,OUTLINE);
    g.seg(p(6,-15),p(8,-27),6,DARK);g.seg(p(5,-15),p(6,-28),2,SHELL);
    g.ellipse(cr+8,CX-29,4,3,LIGHT);g.ellipse(cr+8,CX-30,3,2,GLASS);g.put(cr+7,CX-30,GLINT);
    g.ellipse(cr+23,CX,4,12,OUTLINE);g.ellipse(cr+22,CX,3,12,MID);g.rect(cr+23,cr+32,CX-6,CX+6,DARK);g.rect(cr+23,cr+32,CX-5,CX-3,MID);
    for(const y of [25,28,31])g.rect(cr+y,cr+y,CX-5,CX+5,SHELL);
    g.ellipse(cr+34,CX,3,8,OUTLINE);g.rect(cr+33,cr+36,CX-7,CX+7,DARK);g.rect(cr+33,cr+35,CX-6,CX-4,SHADOW);
    const length=7+flame;
    for(let y=0;y<length;y++){const width=Math.max(0,round(3*(1-y/length)));g.rect(cr+38+y,cr+38+y,CX-width,CX+width,EMBER);if(width)g.rect(cr+38+y,cr+38+y,CX-width+1,CX+width-1,FLAME);if(y<Math.floor(length/2))g.put(cr+38+y,CX,CORE);}
    const left=[p(25,-16),p(42+la,-22),p(54+la,-10),p(63+la,-15),p(63+la,-31),p(52+la,-31)];
    const right=[p(25,16),p(43+ra-lift*.35,14+reach*.25),p(59+ra-lift*.65,19+reach*.25),p(57+ra-lift*.85,31+reach*.35),p(42+ra-lift,33+reach)];
    for(const points of [left,right]){
      for(let i=0;i<points.length-1;i++)g.strut(points[i],points[i+1],i===0?5:2);
      points.forEach((point,i)=>g.joint(point,i<2?3:2));
      const [a,b]=points;for(const t of [.3,.5,.7]){const y=a[0]+t*(b[0]-a[0]),x=a[1]+t*(b[1]-a[1]);g.seg([y,x-2],[y,x+2],1,MID);}
    }
    const sr=cr+50+la,sc=CX-36;
    for(let tooth=0;tooth<12;tooth++){const a=turn+tooth*tau/12;g.seg([sr+6*Math.sin(a),sc+6*Math.cos(a)],[sr+8*Math.sin(a),sc+8*Math.cos(a)],2,SHELL);}
    g.disc(sr,sc,6,MID);g.disc(sr,sc,4,DARK);g.seg([sr+3,sc-5],[sr-1,sc+5],2,SHADOW);g.disc(sr,sc,2,SHELL);g.put(sr-1,sc-1,HI);
    const [wr,wc]=right[right.length-1],opening=5-3*grip;
    for(const side of [-1,1]){const a=[wr-1,wc],b=[wr-5,wc+side*opening],c=[wr-11,wc+side*(opening-1)];g.strut(a,b,2);g.strut(b,c,2);g.seg(c,[c[0]+1,c[1]-side*2],2,SHELL);}
    g.ball(cr,CX);if(voice)g.rect(cr+14,cr+14,CX+1,CX+2+voice,FLAME);
    for(const [side,drift]of[[-1,lp],[1,rp]]){const root=p(-3,side*17),hinge=p(-2,side*24),eye=p(-17+drift+(side===1?2:0),side*26+gaze);g.strut(root,hinge,5);g.strut(hinge,[eye[0]+4,eye[1]],5);g.joint(root,2);g.optic(...eye,9,apertures[side===-1?0:1],glare,side);}
    let pixels=g.g;
    if(tilt){const angle=tilt*Math.PI/180,c=Math.cos(angle),s=Math.sin(angle),pivotY=cr+25;pixels=new Uint8Array(W*H);for(let y=0;y<H;y++)for(let x=0;x<W;x++){const sx=round(CX+(x-CX)*c+(y-pivotY)*s),sy=round(pivotY-(x-CX)*s+(y-pivotY)*c);if(sx>=0&&sx<W&&sy>=0&&sy<H)pixels[y*W+x]=g.g[sy*W+sx];}}
    const data=new Uint8ClampedArray(W*H*4);for(let i=0;i<pixels.length;i++){const m=pixels[i];data.set(PALETTE[m],i*4);data[i*4+3]=m===BG?0:255;}return data;
  }
  const api={width:W,height:H,render};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  root.CodsworthRenderer=api;
})(typeof globalThis!=='undefined'?globalThis:this);
