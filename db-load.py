#!/usr/bin/env python

import subprocess
import sys
import os
from optparse import OptionParser
import re
import StringIO
import pandas

# Our interface to the GraphDB
from bulbs.rexster import Graph, Config, DEBUG

# Our own modules
from gh.connect import Connect

# Bro log files we support
SUPPORTED_BRO_LOGS = ["conn.log", "dns.log", "dpd.log","files.log","ftp.log","http.log","irc.log","notice.log","smtp.log","snmp.log","ssh.log"]

# A per-log dict that contains the list of fields we want to extract, in order
SUPPORTED_BRO_FIELDS = {
    "conn.log": ["ts","uid","id.orig_h","id.orig_p","id.resp_h","id.resp_p","proto","service","duration","orig_bytes","resp_bytes","conn_state","local_orig","missed_bytes","history","orig_pkts","orig_ip_bytes","resp_pkts","resp_ip_bytes","tunnel_parents"],
    "dns.log":["ts","uid","id.orig_h","id.orig_p","id.resp_h","id.resp_p","proto","trans_id","query","qclass","qclass_name","qtype","qtype_name","rcode","rcode_name","AA","TC","RD","RA","Z","answers","TTLs","rejected"],
    "dpd.log": ["ts","uid","id.orig_h","id.orig_p","id.resp_h","id.resp_p","proto","analyzer","failure_reason"],
    "files.log": ["ts","fuid","tx_hosts","rx_hosts","conn_uids","source","depth","analyzers","mime_type","filename","duration","local_orig","is_orig","seen_bytes","total_bytes","missing_bytes","overflow_bytes","timedout","parent_fuid","md5","sha1","sha256","extracted"],
    "ftp.log": ["ts","uid","id.orig_h","id.orig_p","id.resp_h","id.resp_p","user","password","command","arg","mime_type","file_size","reply_code","reply_msg","data_channel.passive","data_channel.orig_h","data_channel.resp_h","data_channel.resp_p","fuid"],
    "http.log": ["ts","uid","id.orig_h","id.orig_p","id.resp_h","id.resp_p","trans_depth","method","host","uri","referrer","user_agent","request_body_len","response_body_len","status_code","status_msg","info_code","info_msg","filename","tags","username","password","proxied","orig_fuids","orig_mime_types","resp_fuids","resp_mime_types"],
    "irc.log": ["ts","uid","id.orig_h","id.orig_p","id.resp_h","id.resp_p","nick","user","command","value","addl","dcc_file_name","dcc_file_size","dcc_mime_type","fuid"],
    "notice.log": ["ts","uid","id.orig_h","id.orig_p","id.resp_h","id.resp_p","fuid","file_mime_type","file_desc","proto","note","msg","sub","src","dst","p","n","peer_descr","actions","suppress_for","dropped","remote_location.country_code","remote_location.region","remote_location.city","remote_location.latitude","remote_location.longitude"],
    "smtp.log": ["ts","uid","id.orig_h","id.orig_p","id.resp_h","id.resp_p","trans_depth","helo","mailfrom","rcptto","date","from","to","reply_to","msg_id","in_reply_to","subject","x_originating_ip","first_received","second_received","last_reply","path","user_agent","tls","fuids","is_webmail"],
    "snmp.log": ["ts","uid","id.orig_h","id.orig_p","id.resp_h","id.resp_p","duration","version","community","get_requests","get_bulk_requests","get_responses","set_requests","display_string","up_since"],
    "ssh.log": ["ts","uid","id.orig_h","id.orig_p","id.resp_h","id.resp_p","status","direction","client","server","remote_location.country_code","remote_location.region","remote_location.city","remote_location.latitude","remote_location.longitude"]
}

# Output date format for timestamps
DATE_FMT="%FT%H:%M:%SZ"

BRO_CUT_CMD=["bro-cut","-U",DATE_FMT]

def is_IP(s):
    return not (re.match("\d+.\d+.\d+.\d+$", s) == None)

def parse_options() :
    parser = OptionParser()
    parser.add_option("-l", "--log-dir", dest="logdir",
                      help="Bro log file directory to parse.")
    parser.add_option("-q", "--quiet", dest="quiet",
                      help="Suppress unecessary output (run quietly)")
    parser.add_option("-o", "--output", dest="outputdir",default=".",
                      help="Output directory (will be created if necessary)")
    (options, args) = parser.parse_args()
    return(options, args)

def readlog(file):

    output = ""

    logtype = file

    logfile = "%s/%s" % (options.logdir,file)

    print "Reading %s..." % logfile
    
    tmp_bro_cut_cmd = BRO_CUT_CMD
    tmp_bro_cut_cmd = tmp_bro_cut_cmd + SUPPORTED_BRO_FIELDS[logtype]

    # Create a job that just cats the log file
    p1 = subprocess.Popen(['cat',logfile], stdout=subprocess.PIPE)

    # This is the bro-cut job, reading the "cat" command output
    p2 = subprocess.Popen(tmp_bro_cut_cmd, stdin=p1.stdout, stdout=subprocess.PIPE)

    p1.stdout.close()

    # Now we're going to use the "pandas" package to create a dataframe
    # out of the log data.  Dataframes greatly simplify the tasks of cleaning
    # the data.
    #
    # StringIO treats the string as a fake file, so we can use pandas to
    # create a dataframe out of the string directly, without having to write
    # it to disk first.
    brodata = StringIO.StringIO(p2.communicate()[0])

    df = pandas.DataFrame.from_csv(brodata, sep="\t", parse_dates=False, header=None, index_col=None)

    df.columns = SUPPORTED_BRO_FIELDS[logtype]

    df.replace(to_replace=["(empty)","-"], value=["",""], inplace=True)

    
    return df

def graph_flows(g, df_conn):
    # Iterate through all the flows
    for con in df_conn.index:
        # For each flow, create new Host objects if necessary.  Then create a
        # new Flow, and add the relationships between the Hosts and the Flow
  
        # Create the source & dest nodes
        src_host = g.host.get_or_create("name",
                                        df_conn.loc[con]["id.orig_h"],
                                        {"name": df_conn.loc[con]["id.orig_h"],
                                         "address":df_conn.loc[con]["id.orig_h"]
                                     })
        dst_host = g.host.get_or_create("name",
                                        df_conn.loc[con]["id.resp_h"],
                                        {"name": df_conn.loc[con]["id.resp_h"],
                                         "address":df_conn.loc[con]["id.resp_h"]
                                     })

        # If the flow is marked "local_orig", we need to update this feature
        # on the source host.  We can't do this at creation time because we
        # might have seen this host before in another context, and created a
        # node for it without knowing it was a local host.
        if df_conn.loc[con]["local_orig"] == "T":
            src_host.local = "T"
            src_host.save()
        
        # Create the Flow object.  Since we can run the same log file through
        # multiple times, or observe the same flow from different log files,
        # assume flows with the same name are actually the same flow.

        flowname = df_conn.loc[con]["uid"]
        # Create the flow node, with all the rich data
        properties = dict(df_conn.loc[con])
        # Manually assign the "name" property
        properties["name"] = flowname
        # Take out the info about the source & dest IPs, since we should be
        # getting them from the connected host nodes
        del properties["id.orig_h"]
        del properties["id.resp_h"]
        
        flow = g.flow.get_or_create("name", flowname, properties)

        # Create the edges for this flow, if they don't already exist
        nodes = flow.inV("source")
        if nodes == None or not (src_host in nodes):
            g.source.create(src_host, flow)
        
        nodes = flow.outV("dest")
        if nodes == None or not (dst_host in nodes):
            g.dest.create(flow, dst_host)

def graph_dns(g, df_dns):
    # Iterate through all the flows
    for i in df_dns.index:
        # Create the DNSTransaction node
        name = str(df_dns.loc[i]["trans_id"])
        timestamp = df_dns.loc[i]["ts"]
        flowname = df_dns.loc[i]["uid"]
        
        properties = dict()
        properties["name"] = name
        # Pick out the properties that belong on the transaction and add
        # them
        transaction = g.dnsTransaction.create(name=name,
                                              ts=df_dns.loc[i]["ts"],
                                              proto=df_dns.loc[i]["proto"],
                                              orig_p=df_dns.loc[i]["id.orig_p"],
                                              resp_p=df_dns.loc[i]["id.resp_p"],
                                              qclass=df_dns.loc[i]["qclass"],
                                              qclass_name=df_dns.loc[i]["qclass_name"],
                                              qtype=df_dns.loc[i]["qtype"],
                                              qtype_name=df_dns.loc[i]["qtype_name"],
                                              rcode=df_dns.loc[i]["rcode"],
                                              rcode_name=df_dns.loc[i]["rcode_name"],
                                              AA=df_dns.loc[i]["AA"],
                                              TC=df_dns.loc[i]["TC"],
                                              RD=df_dns.loc[i]["RD"],
                                              RA=df_dns.loc[i]["RA"],
                                              Z=df_dns.loc[i]["Z"],
                                              rejected=df_dns.loc[i]["rejected"])

        # Create a node + edge for the query, if there is one in the log
        if df_dns.loc[i]["query"]:
            fqdn = g.fqdn.get_or_create("name", df_dns.loc[i]["query"],
                                        {"name":df_dns.loc[i]["query"],
                                         "domain":df_dns.loc[i]["query"]})
            g.lookedUp.create(transaction,fqdn)

            # Now create the nodes and edges for the domains or addresses in
            # the answer (if there is an answer).  There can be multiple
            # answers, so split this into a list and create one node + edge
            # for each.
            #
            # There should also be one TTL per answer, so we'll split those and
            # use array indices to tie them together. The arrays are supposed
            # to always be the same length
            if df_dns.loc[i]["answers"]:
                addrs = df_dns.loc[i]["answers"].split(",")
                ttls = df_dns.loc[i]["TTLs"].split(",")
                for i in range(len(addrs)):
                    ans = addrs[i]
                    ttl = ttls[i]
                    # DNS answers can be either IPs or other names. Figure
                    # out which type of node to create for each answer.
                    if is_IP(ans):
                        node = g.host.get_or_create("name",ans,{"name":ans,
                                                                "address":ans})
                    else:
                        node = g.fqdn.get_or_create("name",ans,{"name":ans,
                                                                "address":ans})
                                                
                    g.resolvedTo.create(fqdn, node, {"ts":timestamp})
                    g.answer.create(transaction, node, {"TTL": ttl})

        # Create a node + edge for the source of the DNS transaction
        # (the client host)
        if df_dns.loc[i]["id.orig_h"]:
            src = g.host.get_or_create("name", df_dns.loc[i]["id.orig_h"],
                                       {"name": df_dns.loc[i]["id.orig_h"],
                                        "address":df_dns.loc[i]["id.orig_h"]})
            g.queried.create(src, transaction)
        
        # Create a node + edge for the destination of the DNS transaction
        # (the DNS server)
        if df_dns.loc[i]["id.resp_h"]:
            dst = g.host.get_or_create("name", df_dns.loc[i]["id.resp_h"],
                                       {"name": df_dns.loc[i]["id.resp_h"],
                                        "address":df_dns.loc[i]["id.resp_h"]})
            g.queriedServer.create(transaction,dst)

        
        # Now connect this transaction to the correct flow
        flows = g.flow.index.lookup(name=flowname)
        if flows == None:
            print "ERROR: Flow '%s' does not exist" % flowname
        else:
            # lookup returns a generator, but since there should only be one
            # flow with this name, just take the first one
            flow = flows.next()
            nodes = flow.outV("resolved")
            if nodes == None or not (transaction in nodes):
                edge = g.resolved.create(flow, transaction)

            
##### Main #####

(options, args) = parse_options()

if not options.logdir:
    print "Error: Must specify the log directory with -l or --log-dir"
    sys.exit(-1)
    
if not os.path.exists(options.logdir):
    print "Error: Directory %s does not exist" % options.logdir
    sys.exit(-1)

if not os.path.exists(options.outputdir):
    os.mkdir(options.outputdir)    
    
if not options.quiet:
    print "Reading log files from %s" % options.logdir

# Now we can start to read data and populate the graph.

g = Connect()

# Now read the types of logs we know how to process, extract the relevant
# data and add it to the graph

df_conn = readlog("conn.log")

print "Graphing Flows..."

graph_flows(g, df_conn)

df_dns = readlog("dns.log")

print "Graphing DNS Transactions..."

graph_dns(g, df_dns)

# Print some basic info about the graph so we know we did some real work
print "Vertices: %d Edges: %d" % ( len(list(g.V)), len(list(g.E)) )
print "\tHosts: %d" % len(list(g.host.get_all()))
print "\tFlows: %d" % len(list(g.flow.get_all()))
print "\tDNSTransactions:   %d" % len(list(g.dnsTransaction.get_all()))
print "\tFQDNs:   %d" % len(list(g.fqdn.get_all()))

print

print "\tSource: %d" % len(list(g.source.get_all()))
print "\tDest:   %d" % len(list(g.dest.get_all()))
print "\tResolved: %d" % len(list(g.resolved.get_all()))
print "\tResolvedTo: %d" % len(list(g.resolvedTo.get_all()))
print "\tLookedUp: %d" % len(list(g.lookedUp.get_all()))
print "\tqueriedServer: %d" % len(list(g.queriedServer.get_all()))

    


