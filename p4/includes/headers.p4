/* -*- P4_16 -*- */
/* Header and metadata definitions for adaptive routing */

#ifndef __HEADERS_P4__
#define __HEADERS_P4__

typedef bit<48> macAddr_t;
typedef bit<32> ip4Addr_t;
typedef bit<9>  port_t;
typedef bit<16> ecmp_group_id_t;
typedef bit<16> ecmp_index_t;

const bit<16> TYPE_IPV4 = 0x0800;
const bit<8>  PROTO_TCP = 6;
const bit<8>  PROTO_UDP = 17;

header ethernet_t {
    macAddr_t dstAddr;
    macAddr_t srcAddr;
    bit<16>   etherType;
}

header ipv4_t {
    bit<4>    version;
    bit<4>    ihl;
    bit<8>    tos;
    bit<16>   totalLen;
    bit<16>   identification;
    bit<3>    flags;
    bit<13>   fragOffset;
    bit<8>    ttl;
    bit<8>    protocol;
    bit<16>   hdrChecksum;
    ip4Addr_t srcAddr;
    ip4Addr_t dstAddr;
}

header tcp_t {
    bit<16> srcPort;
    bit<16> dstPort;
    bit<32> seqNo;
    bit<32> ackNo;
    bit<4>  dataOffset;
    bit<3>  res;
    bit<9>  flags;
    bit<16> window;
    bit<16> checksum;
    bit<16> urgentPtr;
}

header udp_t {
    bit<16> srcPort;
    bit<16> dstPort;
    bit<16> length;
    bit<16> checksum;
}

struct metadata_t {
    ecmp_group_id_t ecmp_group_id;
    ecmp_index_t    ecmp_hash;
    bit<16>         ecmp_count;
    bit<16>         ecmp_base;
    port_t          selected_port;
    port_t          alt_port;
    bit<32>         port_load;
    bit<32>         threshold;
    bit<1>          is_ecmp;
    bit<1>          rerouted;
    bit<16>         l4_srcPort;
    bit<16>         l4_dstPort;
}

struct headers_t {
    ethernet_t ethernet;
    ipv4_t     ipv4;
    tcp_t      tcp;
    udp_t      udp;
}

#endif
