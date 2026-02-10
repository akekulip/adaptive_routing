/* -*- P4_16 -*- */
/*
 * Adaptive Routing with ECMP for BMv2 simple_switch
 *
 * Distributes traffic across multiple equal-cost paths using 5-tuple hashing,
 * with adaptive rerouting when a link's byte count exceeds a threshold.
 */

#include <core.p4>
#include <v1model.p4>

#include "includes/headers.p4"
#include "includes/parsers.p4"

/*************************************************************************
 ***********************  R E G I S T E R S  *****************************
 *************************************************************************/

// Per-port cumulative byte counter (indexed by egress port number)
register<bit<32>>(256) byte_counter;

// Configurable load threshold (single value, set by controller)
register<bit<32>>(1) load_threshold;

/*************************************************************************
 **************  I N G R E S S   P R O C E S S I N G   *******************
 *************************************************************************/

control MyIngress(inout headers_t hdr,
                  inout metadata_t meta,
                  inout standard_metadata_t standard_metadata) {

    action drop() {
        mark_to_drop(standard_metadata);
    }

    // Direct forwarding to a specific next hop (non-ECMP destinations)
    action set_nhop(macAddr_t dstMAC, port_t port) {
        hdr.ethernet.dstAddr = dstMAC;
        standard_metadata.egress_spec = port;
        meta.is_ecmp = 0;
    }

    // Set the ECMP group for this destination
    action set_ecmp_group(ecmp_group_id_t group_id) {
        meta.ecmp_group_id = group_id;
        meta.is_ecmp = 1;
    }

    // Set the ECMP group parameters (count and base index)
    action set_ecmp_info(bit<16> count, bit<16> base) {
        meta.ecmp_count = count;
        meta.ecmp_base = base;

        // Hash on 5-tuple to select ECMP member
        hash(meta.ecmp_hash,
             HashAlgorithm.crc16,
             (bit<16>)0,
             { hdr.ipv4.srcAddr,
               hdr.ipv4.dstAddr,
               hdr.ipv4.protocol,
               meta.l4_srcPort,
               meta.l4_dstPort },
             count);
    }

    // Set the next hop for an ECMP member
    action set_ecmp_nhop(macAddr_t dstMAC, port_t port) {
        hdr.ethernet.dstAddr = dstMAC;
        standard_metadata.egress_spec = port;
        meta.selected_port = port;
    }

    // Reroute to alternative next hop when overloaded
    action set_alt_nhop(macAddr_t dstMAC, port_t port) {
        hdr.ethernet.dstAddr = dstMAC;
        standard_metadata.egress_spec = port;
        meta.alt_port = port;
        meta.rerouted = 1;
    }

    // LPM table: destination IP → next hop or ECMP group
    table ipv4_lpm {
        key = {
            hdr.ipv4.dstAddr: lpm;
        }
        actions = {
            set_nhop;
            set_ecmp_group;
            drop;
        }
        size = 1024;
        default_action = drop();
    }

    // ECMP group table: group_id → count + base
    table ecmp_group {
        key = {
            meta.ecmp_group_id: exact;
        }
        actions = {
            set_ecmp_info;
            drop;
        }
        size = 256;
        default_action = drop();
    }

    // ECMP next-hop table: (group_id, hash_index) → egress port + MAC
    table ecmp_nhop {
        key = {
            meta.ecmp_group_id: exact;
            meta.ecmp_hash: exact;
        }
        actions = {
            set_ecmp_nhop;
            drop;
        }
        size = 1024;
        default_action = drop();
    }

    // Alternative next-hop table: overloaded port → alt port + MAC
    table alt_nhop {
        key = {
            meta.selected_port: exact;
        }
        actions = {
            set_alt_nhop;
            drop;
        }
        size = 256;
        default_action = drop();
    }

    apply {
        if (!hdr.ipv4.isValid()) {
            drop();
            return;
        }

        // Extract L4 ports into metadata for 5-tuple hashing
        if (hdr.tcp.isValid()) {
            meta.l4_srcPort = hdr.tcp.srcPort;
            meta.l4_dstPort = hdr.tcp.dstPort;
        } else if (hdr.udp.isValid()) {
            meta.l4_srcPort = hdr.udp.srcPort;
            meta.l4_dstPort = hdr.udp.dstPort;
        } else {
            meta.l4_srcPort = 0;
            meta.l4_dstPort = 0;
        }

        // Step 1: LPM lookup → direct nhop or ECMP group
        ipv4_lpm.apply();

        if (meta.is_ecmp == 1) {
            // Step 2: Get ECMP group info and compute hash
            ecmp_group.apply();

            // Step 3: Select next hop based on hash
            ecmp_nhop.apply();

            // Step 4: Read current load on selected port
            bit<32> current_load;
            byte_counter.read(current_load, (bit<32>)meta.selected_port);
            meta.port_load = current_load;

            // Step 5: Read threshold
            bit<32> thresh;
            load_threshold.read(thresh, 0);
            meta.threshold = thresh;

            // Step 6: If overloaded, reroute to alternative
            meta.rerouted = 0;
            if (current_load > thresh && thresh > 0) {
                alt_nhop.apply();
            }

            // Step 7: Update byte counter on the final chosen port
            bit<32> final_load;
            port_t final_port;
            if (meta.rerouted == 1) {
                final_port = meta.alt_port;
            } else {
                final_port = meta.selected_port;
            }
            byte_counter.read(final_load, (bit<32>)final_port);
            byte_counter.write((bit<32>)final_port, final_load + (bit<32>)hdr.ipv4.totalLen);
        }

        // Step 8: Set source MAC to switch MAC (placeholder, rewritten per-port)
        hdr.ethernet.srcAddr = 48w0x000000000100;

        // Step 9: Decrement TTL
        hdr.ipv4.ttl = hdr.ipv4.ttl - 1;
        if (hdr.ipv4.ttl == 0) {
            drop();
        }
    }
}

/*************************************************************************
 ****************  E G R E S S   P R O C E S S I N G   *******************
 *************************************************************************/

control MyEgress(inout headers_t hdr,
                 inout metadata_t meta,
                 inout standard_metadata_t standard_metadata) {

    // Rewrite source MAC based on egress port
    action set_smac(macAddr_t srcMAC) {
        hdr.ethernet.srcAddr = srcMAC;
    }

    table smac_rewrite {
        key = {
            standard_metadata.egress_port: exact;
        }
        actions = {
            set_smac;
            NoAction;
        }
        size = 64;
        default_action = NoAction();
    }

    apply {
        smac_rewrite.apply();
    }
}

/*************************************************************************
 ***********************  S W I T C H  ***********************************
 *************************************************************************/

V1Switch(
    MyParser(),
    MyVerifyChecksum(),
    MyIngress(),
    MyEgress(),
    MyComputeChecksum(),
    MyDeparser()
) main;
