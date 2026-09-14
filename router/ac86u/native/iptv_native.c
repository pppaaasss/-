/* Small, short-lived AC86U media sampler. No interpreter, daemon or decoder.
 * libcurl is loaded from the existing curl installation; build needs no target
 * libcurl library. DNS and IPv4 SO_MARK preserve the established home path.
 * All command arguments are data. This program never invokes a shell.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <curl/curl.h>
#include <dirent.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <netdb.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

/* Entware aarch64 uses glibc >=2.27. These APIs were moved into libc in 2.34;
 * explicitly select their original libdl versions when cross-building. */
#if defined(__aarch64__) && defined(__GLIBC__)
__asm__(".symver dlopen,dlopen@GLIBC_2.17");
__asm__(".symver dlsym,dlsym@GLIBC_2.17");
#endif

#define URL_MAX 4096
#define PLAY_MAX 98304
#define PREFIX_MAX 131072
#define MARK_BASE 0x49600000u
static CURL *(*ci)(void);
static CURLcode (*co)(CURL *, CURLoption, ...), (*cp)(CURL *);
static CURLcode (*cg)(CURL *, CURLINFO, ...);
static void (*cc)(CURL *), (*cf)(void *);
static struct curl_slist *(*ca)(struct curl_slist *, const char *);
static void (*cl)(struct curl_slist *);
static CURLU *(*ui)(void);
static CURLUcode (*us)(CURLU *, CURLUPart, const char *, unsigned int);
static CURLUcode (*ug)(CURLU *, CURLUPart, char **, unsigned int);
static void (*uc)(CURLU *);
static void fail(const char *s) { fprintf(stderr,"NATIVE_ERROR:%s\n",s); exit(2); }
static double mono(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return t.tv_sec+t.tv_nsec/1e9; }
static long number(const char *s,long lo,long hi) {
    char *e; errno=0; long n=strtol(s,&e,10);
    if(errno||!*s||*e||n<lo||n>hi) fail("invalid_number");
    return n;
}
static void load_curl(void) {
    void *h=dlopen("libcurl.so.4",RTLD_NOW|RTLD_LOCAL);
    if(!h) h=dlopen("/opt/lib/libcurl.so.4",RTLD_NOW|RTLD_LOCAL);
    if(!h) fail("existing_libcurl_unavailable");
#define LOAD(p,n) do { *(void **)(&(p))=dlsym(h,n); if(!(p)) fail("libcurl_missing_" n); } while(0)
    LOAD(ci,"curl_easy_init"); LOAD(co,"curl_easy_setopt"); LOAD(cp,"curl_easy_perform");
    LOAD(cg,"curl_easy_getinfo"); LOAD(cc,"curl_easy_cleanup"); LOAD(cf,"curl_free");
    LOAD(ca,"curl_slist_append"); LOAD(cl,"curl_slist_free_all");
    LOAD(ui,"curl_url"); LOAD(us,"curl_url_set"); LOAD(ug,"curl_url_get"); LOAD(uc,"curl_url_cleanup");
}
static void safe_url(const char *s) {
    if(strlen(s)>=URL_MAX || (strncmp(s,"http://",7)&&strncmp(s,"https://",8))) fail("invalid_url");
    for(;*s;s++) if((unsigned char)*s<32 || (unsigned char)*s==127) fail("url_control_character");
}
static void joined(const char *base,const char *relative,char *out) {
    CURLU *u=ui(); char *s=NULL,*user=NULL;
    if(!u||us(u,CURLUPART_URL,base,0)||us(u,CURLUPART_URL,relative,0)||ug(u,CURLUPART_URL,&s,0)) fail("url_join");
    if(!ug(u,CURLUPART_USER,&user,0)) { cf(user); fail("url_credentials"); }
    safe_url(s); snprintf(out,URL_MAX,"%s",s); cf(s); uc(u);
}
static uint16_t u16(const unsigned char *p) { return ((uint16_t)p[0]<<8)|p[1]; }
static int skip_name(const unsigned char *b,int n,int p) {
    for(int count=0;count<128 && p<n;count++) {
        int z=b[p++]; if(!z) return p;
        if((z&192)==192) return p<n?p+1:-1;
        if(z>63||p+z>n) return -1;
        p+=z;
    }
    return -1;
}
static int all_io(int fd,unsigned char *p,size_t n,int sending,double end) {
    size_t done=0;
    while(done<n) {
        double left=end-mono(); if(left<=0) return -1;
        struct pollfd f={fd,sending?POLLOUT:POLLIN,0};
        if(poll(&f,1,(int)(left*1000))<=0) return -1;
        ssize_t k=sending?send(fd,p+done,n-done,MSG_NOSIGNAL):recv(fd,p+done,n-done,0);
        if(k<=0) return -1;
        done+=(size_t)k;
    }
    return 0;
}
/* RFC 1035: explicit LAN resolver, A+AAAA, matching question and transaction,
 * UDP with TCP fallback on truncation. Neither DNS socket gets a media mark. */
static int dns_query(const char *host,const char *server,int port,int type,char ips[16][INET6_ADDRSTRLEN],int count) {
    unsigned char q[512]={0},b[4096]; int len=12;
    unsigned short id=(unsigned short)(getpid() ^ (long)(mono()*1000000));
    q[0]=id>>8; q[1]=id&255; q[2]=1; q[5]=1;
    const char *p=host;
    while(*p) { const char *dot=strchr(p,'.'); size_t z=dot?(size_t)(dot-p):strlen(p);
        if(!z||z>63||len+(int)z+6>512) return -1;
        q[len++]=(unsigned char)z; memcpy(q+len,p,z); len+=(int)z;
        if(!dot) break;
        p=dot+1;
    }
    q[len++]=0; q[len++]=0; q[len++]=(unsigned char)type; q[len++]=0; q[len++]=1;
    struct sockaddr_in sa={.sin_family=AF_INET,.sin_port=htons((uint16_t)port)};
    if(inet_pton(AF_INET,server,&sa.sin_addr)!=1) return -1;
    int fd=socket(AF_INET,SOCK_DGRAM|SOCK_CLOEXEC,0); if(fd<0) return -1;
    double end=mono()+2;
    if(connect(fd,(struct sockaddr *)&sa,sizeof sa)||send(fd,q,len,0)!=len) {close(fd);return -1;}
    struct pollfd f={fd,POLLIN,0}; int n=-1;
    if(poll(&f,1,1900)>0) n=(int)recv(fd,b,sizeof b,0);
    close(fd);
    if(n<12||u16(b)!=id||!(b[2]&128)||b[2]&0x78) return -1;
    if(b[2]&2) {
        fd=socket(AF_INET,SOCK_STREAM|SOCK_CLOEXEC|SOCK_NONBLOCK,0); if(fd<0) return -1;
        int r=connect(fd,(struct sockaddr *)&sa,sizeof sa);
        struct pollfd pf={fd,POLLOUT,0}; int err=0; socklen_t es=sizeof err;
        if((r&&errno!=EINPROGRESS)||poll(&pf,1,1500)<=0||getsockopt(fd,SOL_SOCKET,SO_ERROR,&err,&es)||err) {close(fd);return -1;}
        end=mono()+2; unsigned char size[2]={(unsigned char)(len>>8),(unsigned char)len};
        if(all_io(fd,size,2,1,end)||all_io(fd,q,len,1,end)||all_io(fd,size,2,0,end)) {close(fd);return -1;}
        n=u16(size); if(n<12||n>(int)sizeof b||all_io(fd,b,n,0,end)) {close(fd);return -1;} close(fd);
    }
    if(n<len||u16(b)!=id||u16(b+4)!=1||memcmp(b+12,q+12,len-12)) return -1;
    if((b[3]&15)==3) return count;
    if((b[3]&15)!=0) return -1;
    int pos=len, answers=u16(b+6);
    if(answers>128) return -1;
    for(int i=0;i<answers;i++) {
        pos=skip_name(b,n,pos); if(pos<0||pos+10>n) return -1;
        int kind=u16(b+pos), cls=u16(b+pos+2),z=u16(b+pos+8); pos+=10;
        if(pos+z>n) return -1;
        if(cls==1 && count<16 && ((kind==1&&z==4)||(kind==28&&z==16))) {
            if(inet_ntop(kind==1?AF_INET:AF_INET6,b+pos,ips[count],INET6_ADDRSTRLEN)) count++;
        }
        pos+=z;
    }
    return count;
}
static struct curl_slist *resolve(const char *url,const char *dns,int dns_port) {
    CURLU *u=ui(); char *host=NULL,*port=NULL,*user=NULL;
    if(!u||us(u,CURLUPART_URL,url,0)||ug(u,CURLUPART_HOST,&host,0)||ug(u,CURLUPART_PORT,&port,CURLU_DEFAULT_PORT)) fail("url_parse");
    if(!ug(u,CURLUPART_USER,&user,0)) {cf(user);fail("url_credentials");}
    char ips[16][INET6_ADDRSTRLEN],entry[2048],numeric[URL_MAX]; unsigned char addr[16];
    snprintf(numeric,sizeof numeric,"%s",host);
    if(numeric[0]=='[') {memmove(numeric,numeric+1,strlen(numeric));char *end=strchr(numeric,']');if(end)*end=0;}
    int count=0;
    if(inet_pton(AF_INET,numeric,addr)==1||inet_pton(AF_INET6,numeric,addr)==1) {cf(host);cf(port);uc(u);return NULL;}
    int six=dns_query(host,dns,dns_port,28,ips,0);
    if(six>0) count=six;
    int four=dns_query(host,dns,dns_port,1,ips,count);
    if(four>0) count=four;
    if(!count) {cf(host);cf(port);uc(u);return NULL;}
    int used=snprintf(entry,sizeof entry,"%s:%s:",host,port);
    for(int i=0;i<count;i++) used+=snprintf(entry+used,sizeof entry-(size_t)used,"%s%s%s%s",i?",":"",strchr(ips[i],':')?"[":"",ips[i],strchr(ips[i],':')?"]":"");
    struct curl_slist *list=ca(NULL,entry); cf(host);cf(port);uc(u);return list;
}
static int rule(const char *action,uint32_t mark) {
    char value[48];snprintf(value,sizeof value,"%u/0xffffffff",mark);
    pid_t p=fork();if(p<0)return -1;
    if(!p){int fd=open("/dev/null",O_WRONLY);if(fd>=0){dup2(fd,1);dup2(fd,2);close(fd);}
        execlp("iptables","iptables","-t","nat",action,"OUTPUT","-p","tcp","-m","mark","--mark",value,"-j","merlinclash",(char *)NULL);_exit(127);}
    int s;while(waitpid(p,&s,0)<0){if(errno!=EINTR)return -1;}return WIFEXITED(s)?WEXITSTATUS(s):2;
}
struct transfer { FILE *file; size_t count,kept,limit,keep; int capped,local_error; long status; long long total; char location[URL_MAX]; uint32_t mark; };
static curl_socket_t open_socket(void *v,curlsocktype purpose,struct curl_sockaddr *a) {
    (void)purpose; struct transfer *t=v;
    if(t->mark && rule("-C",t->mark)){t->local_error=1;return CURL_SOCKET_BAD;}
    int fd=socket(a->family,a->socktype|SOCK_CLOEXEC,a->protocol);
    if(fd>=0 && a->family==AF_INET && t->mark && setsockopt(fd,SOL_SOCKET,SO_MARK,&t->mark,sizeof t->mark)) {t->local_error=1;close(fd);return CURL_SOCKET_BAD;}
    return fd;
}
static size_t body(void *p,size_t a,size_t b,void *v) {
    struct transfer *t=v; size_t n=a*b;
    if(t->status>=300&&t->status<400) return n;
    size_t take=n; if(take>t->limit-t->count) take=t->limit-t->count;
    size_t keep=take; if(keep>t->keep-t->kept) keep=t->keep-t->kept;
    if(keep && fwrite(p,1,keep,t->file)!=keep) return 0;
    t->count+=take;t->kept+=keep;
    if(t->count>=t->limit) {t->capped=1;return 0;} return n;
}
static size_t header(void *p,size_t a,size_t b,void *v) {
    struct transfer *t=v; size_t n=a*b; char s[URL_MAX+64];
    if(n>=sizeof s) return 0;
    memcpy(s,p,n);s[n]=0;
    if(!strncmp(s,"HTTP/",5)) {char *z=strchr(s,' ');t->status=z?strtol(z+1,NULL,10):0;t->location[0]=0;t->total=0;}
    if(!strncasecmp(s,"Content-Range:",14)) {char *z=strrchr(s,'/');if(z)t->total=strtoll(z+1,NULL,10);}
    else if(!strncasecmp(s,"Content-Length:",15)&&!t->total)t->total=strtoll(s+15,NULL,10);
    else if(!strncasecmp(s,"Location:",9)) {
        char *z=s+9;while(*z==' '||*z=='\t')z++;z[strcspn(z,"\r\n")]=0;
        size_t len=strlen(z); if(len>=URL_MAX)return 0;
        memcpy(t->location,z,len+1);
    }
    return n;
}
static int get(int argc,char **argv) {
    if(argc!=10) fail("get_arguments");
    safe_url(argv[2]); long limit=number(argv[3],65536,6*1024*1024),keep=number(argv[4],0,PLAY_MAX>PREFIX_MAX?PLAY_MAX:PREFIX_MAX);
    long mark=number(argv[7],0,2147483647); if(mark && ((unsigned long)mark&0xffff0000u)!=MARK_BASE) fail("invalid_media_mark");
    int dns_port=(int)number(argv[9],1,65535); load_curl();
    char url[URL_MAX];snprintf(url,sizeof url,"%s",argv[2]); double started=mono();
    struct transfer t={.limit=(size_t)limit,.keep=(size_t)keep,.mark=(uint32_t)mark};
    t.file=fopen(argv[5],"wb");if(!t.file)fail("sample_file");
    CURLcode code=CURLE_OK;
    for(int redirects=0;redirects<5;redirects++) {
        CURL *c=ci();if(!c)fail("curl_init");
        struct curl_slist *hosts=resolve(url,argv[8],dns_port);
        /* Never silently fall back to the runner/router's unrelated DNS. */
        CURLU *u=ui();char *host=NULL;us(u,CURLUPART_URL,url,0);ug(u,CURLUPART_HOST,&host,0);
        unsigned char numeric[16];char h[URL_MAX];snprintf(h,sizeof h,"%s",host?host:"");
        if(h[0]=='['){memmove(h,h+1,strlen(h));char *e=strchr(h,']');if(e)*e=0;}
        int literal=inet_pton(AF_INET,h,numeric)==1||inet_pton(AF_INET6,h,numeric)==1;cf(host);uc(u);
        if(!literal&&!hosts){code=CURLE_COULDNT_RESOLVE_HOST;cc(c);break;}
        co(c,CURLOPT_URL,url);co(c,CURLOPT_PROXY,"");co(c,CURLOPT_NOPROXY,"*");
        co(c,CURLOPT_PROTOCOLS,(long)(CURLPROTO_HTTP|CURLPROTO_HTTPS));
        co(c,CURLOPT_RESOLVE,hosts);co(c,CURLOPT_NOSIGNAL,1L);co(c,CURLOPT_CONNECTTIMEOUT_MS,5000L);
        long remain=(long)((14-(mono()-started))*1000);if(remain<1){cl(hosts);cc(c);code=CURLE_OPERATION_TIMEDOUT;break;}
        co(c,CURLOPT_TIMEOUT_MS,remain);co(c,CURLOPT_BUFFERSIZE,16384L);
        co(c,CURLOPT_USERAGENT,"AC86U-Native-Probe/1.0");
        co(c,CURLOPT_SSL_VERIFYPEER,1L);co(c,CURLOPT_SSL_VERIFYHOST,2L);
        const char *ca_file=getenv("IPTV_CA_FILE");if(ca_file&&*ca_file)co(c,CURLOPT_CAINFO,ca_file);
        co(c,CURLOPT_OPENSOCKETFUNCTION,open_socket);co(c,CURLOPT_OPENSOCKETDATA,&t);
        co(c,CURLOPT_WRITEFUNCTION,body);co(c,CURLOPT_WRITEDATA,&t);
        co(c,CURLOPT_HEADERFUNCTION,header);co(c,CURLOPT_HEADERDATA,&t);
        char range[40];snprintf(range,sizeof range,"0-%ld",limit-1);
        if(keep==PREFIX_MAX)co(c,CURLOPT_RANGE,range);
        code=cp(c);cl(hosts);cc(c);
        if(code || t.status<300 || t.status>=400 || !t.location[0])break;
        char next[URL_MAX];joined(url,t.location,next);snprintf(url,sizeof url,"%s",next);
        if(redirects==4)code=CURLE_TOO_MANY_REDIRECTS;
    }
    if(fclose(t.file))fail("sample_flush");
    if(t.capped&&code==CURLE_WRITE_ERROR)code=CURLE_OK;
    if(!t.total&&!t.capped&&code==CURLE_OK)t.total=(long long)t.count;
    FILE *f=fopen(argv[6],"w");if(!f)fail("metric_file");
    fprintf(f,"%d\t%ld\t%zu\t%.6f\t%lld\t%d\t%s\n",t.local_error?1000:(int)code,t.status,t.count,fmax(.001,mono()-started),t.total,
            t.total>0&&(long long)t.count>=t.total,url);
    fclose(f);return 0;
}
/* Local playlist parsing only. Unsupported encryption/byte ranges stay UNKNOWN. */
static int hls(int argc,char **argv) {
    if(argc!=4)fail("hls_arguments");
    load_curl();FILE *f=fopen(argv[2],"rb");if(!f)fail("playlist_file");
    char *doc=calloc(PLAY_MAX+1,1);if(!doc)fail("playlist_memory");size_t n=fread(doc,1,PLAY_MAX,f);fclose(f);
    if(strncmp(doc,"#EXTM3U",7)){puts("DIRECT");free(doc);return 0;}
    if(n>=PLAY_MAX){free(doc);return 3;}
    char *line,*save=NULL,variant[URL_MAX]="",segments[2][URL_MAX]={{0}};
    double durations[2]={0},duration=0;long best_h=-1,best_b=-1,h=0,bw=0;int pending=0,seen=0;
    for(line=strtok_r(doc,"\n",&save);line;line=strtok_r(NULL,"\n",&save)) {
        line[strcspn(line,"\r")]=0;while(*line==' '||*line=='\t')line++;
        if(strstr(line,"#EXT-X-BYTERANGE")||strstr(line,"#EXT-X-PART:")||strstr(line,"#EXT-X-MAP:")||
           (strstr(line,"#EXT-X-KEY:")&&!strstr(line,"METHOD=NONE"))) {puts("UNSUPPORTED");free(doc);return 0;}
        if(!strncmp(line,"#EXT-X-STREAM-INF:",18)) {
            char *r=strstr(line,"RESOLUTION="),*b=strstr(line,"BANDWIDTH=");
            h=0;bw=b?strtol(b+10,NULL,10):0;if(r){r=strchr(r,'x');if(r)h=strtol(r+1,NULL,10);}pending=1;
        } else if(!strncmp(line,"#EXTINF:",8)) {duration=strtod(line+8,NULL);if(!isfinite(duration)||duration<0||duration>3600)duration=0;}
        else if(*line&&*line!='#') {
            char u[URL_MAX];joined(argv[3],line,u);
            if(pending) {if(h>best_h||(h==best_h&&bw>best_b)){snprintf(variant,sizeof variant,"%s",u);best_h=h;best_b=bw;}pending=0;}
            else if(strcmp(u,segments[1])) {snprintf(segments[0],URL_MAX,"%s",segments[1]);durations[0]=durations[1];snprintf(segments[1],URL_MAX,"%s",u);durations[1]=duration;seen++;duration=0;}
        }
    }
    if(*variant)printf("MASTER\t%s\n",variant);
    else if(seen>=2)printf("SEGMENT\t%.6f\t%s\nSEGMENT\t%.6f\t%s\n",durations[0],segments[0],durations[1],segments[1]);
    else puts("UNSUPPORTED");
    free(doc);return 0;
}
static long available(void) {
    FILE *f=fopen("/proc/meminfo","r");char line[256];long n=-1;
    if(!f)return -1;
    while(fgets(line,sizeof line,f))if(sscanf(line,"MemAvailable: %ld",&n)==1)break;
    fclose(f);return n;
}
static int cpu(unsigned long long *total,unsigned long long *idle) {
    FILE *f=fopen("/proc/stat","r");unsigned long long a[8]={0};if(!f)return -1;
    int n=fscanf(f,"cpu %llu %llu %llu %llu %llu %llu %llu %llu",a,a+1,a+2,a+3,a+4,a+5,a+6,a+7);fclose(f);
    if(n<4)return -1;
    *total=0;for(int i=0;i<8;i++)*total+=a[i];*idle=a[3]+a[4];return 0;
}
static long tree_rss(pid_t p,int depth) {
    if(depth>12)return 0;
    char path[128],s[256];long total=0;snprintf(path,sizeof path,"/proc/%ld/status",(long)p);
    FILE *f=fopen(path,"r");if(f){while(fgets(s,sizeof s,f)){long r;if(sscanf(s,"VmRSS: %ld",&r)==1)total=r;}fclose(f);}
    snprintf(path,sizeof path,"/proc/%ld/task/%ld/children",(long)p,(long)p);f=fopen(path,"r");
    if(f){long child;int count=0;while(count++<32&&fscanf(f,"%ld",&child)==1)total+=tree_rss((pid_t)child,depth+1);fclose(f);}return total;
}
static volatile sig_atomic_t stopped=0;
static void stop(int s){stopped=s;}
static int lockfile(const char *root,const char *name) {
    char p[1024];snprintf(p,sizeof p,"%s/%s",root,name);int fd=open(p,O_CREAT|O_RDWR,0600);
    if(fd<0||flock(fd,LOCK_EX|LOCK_NB)){if(fd>=0)close(fd);return -1;}return fd;
}
static int guard(int argc,char **argv) {
    if(argc<9)fail("guard_arguments");
    long start=number(argv[3],50*1024,256*1024),reserve=number(argv[4],48*1024,start),rss=number(argv[5],4096,32768),seconds=number(argv[6],1,240);
    const char *root=getenv("IPTV_NATIVE_DATA"),*legacy=getenv("IPTV_NATIVE_LEGACY");
    if(root&&lockfile(root,"worker.lock")<0)return 0;
    if(root&&lockfile(legacy?legacy:"/opt/var/lib/iptv-home-probe","daily-worker.lock")<0)return 0;
    long mem=available(),minimum=mem,peak=tree_rss(getpid(),0);FILE *metrics=fopen(argv[2],"w");if(!metrics)fail("guard_metrics");
    unsigned long long t0,i0,t1,i1;int bad=cpu(&t0,&i0);usleep(250000);bad|=cpu(&t1,&i1);
    if(mem<start||bad||t1<=t0||100.0*(1-(double)(i1-i0)/(t1-t0))>=75){fprintf(metrics,"WAITING_RESOURCES\t%ld\t%ld\t0\n",peak,minimum);fclose(metrics);return 75;}
    signal(SIGTERM,stop);signal(SIGINT,stop);signal(SIGHUP,stop);
    double began=mono();pid_t pid=fork();if(pid<0)fail("guard_fork");
    if(!pid){setpgid(0,0);setpriority(PRIO_PROCESS,0,19);execv(argv[7],argv+7);_exit(127);}
    setpgid(pid,pid);int status=0;const char *reason="COMPLETED";double last_cpu=mono();int busy=0;
    for(;;) {
        pid_t waited=waitpid(pid,&status,WNOHANG);
        if(waited==pid)break;
        if(waited<0&&errno!=EINTR){reason="WAIT_ERROR";break;}
        long used=tree_rss(getpid(),0);mem=available();if(used>peak)peak=used;if(mem<minimum)minimum=mem;
        if(mono()-last_cpu>=1) {
            t0=t1;i0=i1;
            if(cpu(&t1,&i1)||t1<=t0||i1<i0)busy=2;
            else busy=100.0*(1-(double)(i1-i0)/(t1-t0))>=75?busy+1:0;
            last_cpu=mono();
        }
        if(stopped||mem<reserve||used>rss||busy>=2||mono()-began>seconds) {
            reason=stopped?"INTERRUPTED":mem<reserve?"MEMORY_RESERVE":used>rss?"RSS_LIMIT":busy>=2?"CPU_BUSY":"TIME_LIMIT";
            kill(-pid,SIGTERM);usleep(500000);kill(-pid,SIGKILL);while(waitpid(pid,&status,0)<0&&errno==EINTR){}break;
        }
        usleep(100000);
    }
    if(root) {
        uint32_t mark=MARK_BASE|((uint32_t)pid&65535);
        int present=rule("-C",mark);
        if((present==0 && rule("-D",mark)!=0)||present>1||present<0) {
            reason="ROUTE_CLEANUP_FAILED";
            char paused[1024];snprintf(paused,sizeof paused,"%s/PAUSED",root);
            int fd=open(paused,O_CREAT|O_WRONLY,0600);if(fd>=0)close(fd);
        }
    }
    fprintf(metrics,"%s\t%ld\t%ld\t%.3f\n",reason,peak,minimum,mono()-began);fclose(metrics);
    return strcmp(reason,"COMPLETED")?75:WIFEXITED(status)?WEXITSTATUS(status):2;
}
int main(int argc,char **argv) {
    signal(SIGPIPE,SIG_IGN);
    if(argc<2)fail("command_required");
    if(!strcmp(argv[1],"get"))return get(argc,argv);
    if(!strcmp(argv[1],"hls"))return hls(argc,argv);
    if(!strcmp(argv[1],"guard"))return guard(argc,argv);
    if(!strcmp(argv[1],"lock")) {
        if(argc<6)fail("lock_arguments");
        if(lockfile(argv[2],"worker.lock")<0||lockfile(argv[3],"background-upgrade.lock")<0||
           lockfile(argv[3],"daily-worker.lock")<0)fail("worker_active");
        if(access("/opt/var/run/iptv-home-probe.lock",F_OK)==0)fail("legacy_manual_worker_active");
        execv(argv[4],argv+4);fail("lock_exec");
    }
    if(!strcmp(argv[1],"commit")) {
        if(argc!=4)fail("commit_arguments");
        int fd=open(argv[2],O_RDONLY);if(fd<0||fsync(fd))fail("checkpoint_sync");close(fd);
        if(rename(argv[2],argv[3]))fail("checkpoint_rename");
        char path[4096];snprintf(path,sizeof path,"%s",argv[3]);char *slash=strrchr(path,'/');
        if(slash){*slash=0;fd=open(*path?path:"/",O_RDONLY|O_DIRECTORY);if(fd<0||fsync(fd))fail("checkpoint_directory_sync");close(fd);}return 0;
    }
    if(!strcmp(argv[1],"check")){load_curl();puts("IPTV_NATIVE_V1_OK");return 0;}
    fail("unknown_command");return 2;
}
