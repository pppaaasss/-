"""Test-only libc transport virtualization; never included in router builds.

The sampler validates a public IPv4 address unchanged. Only this shared test
library redirects its final connect to the in-process loopback HTTP fixture.
No real public TCP connection is allowed by this fixture.
"""
from pathlib import Path
import subprocess

PUBLIC = '93.184.216.34'


def build_transport(directory):
    directory=Path(directory)
    source=directory/'fixture-connect.c'
    library=directory/'fixture-connect.so'
    source.write_text(r'''
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <string.h>
#include <sys/socket.h>
int connect(int fd,const struct sockaddr *address,socklen_t length) {
    int (*original)(int,const struct sockaddr *,socklen_t)=dlsym(RTLD_NEXT,"connect");
    if(address->sa_family==AF_INET && length>=sizeof(struct sockaddr_in)) {
        struct sockaddr_in target=*(const struct sockaddr_in *)address;
        unsigned long ip=ntohl(target.sin_addr.s_addr);
        if(ip==0x5db8d822u)target.sin_addr.s_addr=htonl(0x7f000001u);
        else if((ip>>24)!=127){errno=EACCES;return -1;}
        return original(fd,(const struct sockaddr *)&target,sizeof target);
    }
    if(address->sa_family==AF_INET6){errno=EACCES;return -1;}
    return original(fd,address,length);
}
''')
    subprocess.run(['cc','-shared','-fPIC','-Wall','-Wextra','-Werror','-o',str(library),str(source),'-ldl'],check=True)
    return str(library)
