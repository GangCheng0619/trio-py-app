# This is a public namespace, so we don't want to expose any non-underscored
# attributes that aren't actually part of our public API. But it's very
# annoying to carefully always use underscored names for module-level
# temporaries, imports, etc. when implementing the module. So we put the
# implementation in an underscored module, and then re-export the public parts
# here.
# We still have some underscore names though but only a few.

from . import _socket
import sys as _sys

try:
    from ._socket import (
        CMSG_LEN, CMSG_SPACE, CAPI, AF_UNSPEC, AF_INET, AF_UNIX, AF_IPX,
        AF_APPLETALK, AF_INET6, AF_ROUTE, AF_LINK, AF_SNA, PF_SYSTEM,
        AF_SYSTEM, SOCK_STREAM, SOCK_DGRAM, SOCK_RAW, SOCK_SEQPACKET, SOCK_RDM,
        SO_DEBUG, SO_ACCEPTCONN, SO_REUSEADDR, SO_KEEPALIVE, SO_DONTROUTE,
        SO_BROADCAST, SO_USELOOPBACK, SO_LINGER, SO_OOBINLINE, SO_REUSEPORT,
        SO_SNDBUF, SO_RCVBUF, SO_SNDLOWAT, SO_RCVLOWAT, SO_SNDTIMEO,
        SO_RCVTIMEO, SO_ERROR, SO_TYPE, LOCAL_PEERCRED, SOMAXCONN, SCM_RIGHTS,
        SCM_CREDS, MSG_OOB, MSG_PEEK, MSG_DONTROUTE, MSG_DONTWAIT, MSG_EOR,
        MSG_TRUNC, MSG_CTRUNC, MSG_WAITALL, MSG_EOF, SOL_SOCKET, SOL_IP,
        SOL_TCP, SOL_UDP, IPPROTO_IP, IPPROTO_HOPOPTS, IPPROTO_ICMP,
        IPPROTO_IGMP, IPPROTO_GGP, IPPROTO_IPV4, IPPROTO_IPIP, IPPROTO_TCP,
        IPPROTO_EGP, IPPROTO_PUP, IPPROTO_UDP, IPPROTO_IDP, IPPROTO_HELLO,
        IPPROTO_ND, IPPROTO_TP, IPPROTO_ROUTING, IPPROTO_FRAGMENT,
        IPPROTO_RSVP, IPPROTO_GRE, IPPROTO_ESP, IPPROTO_AH, IPPROTO_ICMPV6,
        IPPROTO_NONE, IPPROTO_DSTOPTS, IPPROTO_XTP, IPPROTO_EON, IPPROTO_PIM,
        IPPROTO_IPCOMP, IPPROTO_SCTP, IPPROTO_RAW, IPPROTO_MAX,
        SYSPROTO_CONTROL, IPPORT_RESERVED, IPPORT_USERRESERVED, INADDR_ANY,
        INADDR_BROADCAST, INADDR_LOOPBACK, INADDR_UNSPEC_GROUP,
        INADDR_ALLHOSTS_GROUP, INADDR_MAX_LOCAL_GROUP, INADDR_NONE, IP_OPTIONS,
        IP_HDRINCL, IP_TOS, IP_TTL, IP_RECVOPTS, IP_RECVRETOPTS,
        IP_RECVDSTADDR, IP_RETOPTS, IP_MULTICAST_IF, IP_MULTICAST_TTL,
        IP_MULTICAST_LOOP, IP_ADD_MEMBERSHIP, IP_DROP_MEMBERSHIP,
        IP_DEFAULT_MULTICAST_TTL, IP_DEFAULT_MULTICAST_LOOP,
        IP_MAX_MEMBERSHIPS, IPV6_JOIN_GROUP, IPV6_LEAVE_GROUP,
        IPV6_MULTICAST_HOPS, IPV6_MULTICAST_IF, IPV6_MULTICAST_LOOP,
        IPV6_UNICAST_HOPS, IPV6_V6ONLY, IPV6_CHECKSUM, IPV6_RECVTCLASS,
        IPV6_RTHDR_TYPE_0, IPV6_TCLASS, TCP_NODELAY, TCP_MAXSEG, TCP_KEEPINTVL,
        TCP_KEEPCNT, TCP_FASTOPEN, TCP_NOTSENT_LOWAT, EAI_ADDRFAMILY,
        EAI_AGAIN, EAI_BADFLAGS, EAI_FAIL, EAI_FAMILY, EAI_MEMORY, EAI_NODATA,
        EAI_NONAME, EAI_OVERFLOW, EAI_SERVICE, EAI_SOCKTYPE, EAI_SYSTEM,
        EAI_BADHINTS, EAI_PROTOCOL, EAI_MAX, AI_PASSIVE, AI_CANONNAME,
        AI_NUMERICHOST, AI_NUMERICSERV, AI_MASK, AI_ALL, AI_V4MAPPED_CFG,
        AI_ADDRCONFIG, AI_V4MAPPED, AI_DEFAULT, NI_MAXHOST, NI_MAXSERV,
        NI_NOFQDN, NI_NUMERICHOST, NI_NAMEREQD, NI_NUMERICSERV, NI_DGRAM,
        SHUT_RD, SHUT_WR, SHUT_RDWR, EBADF, EAGAIN, EWOULDBLOCK, AF_ASH,
        AF_ATMPVC, AF_ATMSVC, AF_AX25, AF_BLUETOOTH, AF_BRIDGE, AF_ECONET,
        AF_IRDA, AF_KEY, AF_LLC, AF_NETBEUI, AF_NETLINK, AF_NETROM, AF_PACKET,
        AF_PPPOX, AF_ROSE, AF_SECURITY, AF_WANPIPE, AF_X25, BDADDR_ANY,
        BDADDR_LOCAL, FD_SETSIZE, IPV6_DSTOPTS, IPV6_HOPLIMIT, IPV6_HOPOPTS,
        IPV6_NEXTHOP, IPV6_PKTINFO, IPV6_RECVDSTOPTS, IPV6_RECVHOPLIMIT,
        IPV6_RECVHOPOPTS, IPV6_RECVPKTINFO, IPV6_RECVRTHDR, IPV6_RTHDR,
        IPV6_RTHDRDSTOPTS, MSG_ERRQUEUE, NETLINK_DNRTMSG, NETLINK_FIREWALL,
        NETLINK_IP6_FW, NETLINK_NFLOG, NETLINK_ROUTE, NETLINK_USERSOCK,
        NETLINK_XFRM, PACKET_BROADCAST, PACKET_FASTROUTE, PACKET_HOST,
        PACKET_LOOPBACK, PACKET_MULTICAST, PACKET_OTHERHOST, PACKET_OUTGOING,
        POLLERR, POLLHUP, POLLIN, POLLMSG, POLLNVAL, POLLOUT, POLLPRI,
        POLLRDBAND, POLLRDNORM, POLLWRNORM, SIOCGIFINDEX, SIOCGIFNAME,
        SOCK_CLOEXEC, TCP_CORK, TCP_DEFER_ACCEPT, TCP_INFO, TCP_KEEPIDLE,
        TCP_LINGER2, TCP_QUICKACK, TCP_SYNCNT, TCP_WINDOW_CLAMP
    )
except ImportError:
    pass

# expose all uppercase names from standardlib socket to trio.socket
import socket as _stdlib_socket

globals().update(
    {
        _name: _value
        for (_name, _value) in _stdlib_socket.__dict__.items()
        if _name.isupper() and not _name.startswith('_')
    }
)

# import the overwrites
from ._socket import (
    fromfd, from_stdlib_socket, getprotobyname, socketpair, getnameinfo,
    socket, getaddrinfo, set_custom_hostname_resolver,
    set_custom_socket_factory, SocketType
)

# not always available so expose only if
try:
    from ._socket import fromshare
except ImportError:
    pass

# expose these functions to trio.socket
from socket import (
    gaierror,
    herror,
    gethostname,
    ntohs,
    htonl,
    htons,
    inet_aton,
    inet_ntoa,
    inet_pton,
    inet_ntop,
)

# not always available so expose only if
try:
    from socket import (
        sethostname, if_nameindex, if_nametoindex, if_indextoname
    )
except ImportError:
    pass

if _sys.platform == 'win32':
    # See https://github.com/python-trio/trio/issues/39
    # Do not import for windows platform
    # (you can still get it from stdlib socket, of course, if you want it)
    del SO_REUSEADDR

# get names used by trio that we define on our own
from ._socket import IPPROTO_IPV6

# Not defined in all python versions and platforms but sometimes needed
try:
    TCP_NOTSENT_LOWAT
except NameError:
    # Hopefully will show up in 3.7:
    #   https://github.com/python/cpython/pull/477
    if _sys.platform == "darwin":
        TCP_NOTSENT_LOWAT = 0x201
    elif _sys.platform == "linux":
        TCP_NOTSENT_LOWAT = 25
