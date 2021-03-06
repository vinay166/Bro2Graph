#!/usr/bin/env python

import subprocess
import sys
import os
from optparse import OptionParser
import re
import StringIO
import numpy
import pandas
import random
import string

# Our interface to the GraphDB
from bulbs.rexster import Graph, Config, DEBUG

# Our own modules
from gh.connect import Connect
from gh.util import graph_info, shortest_path, edge_list
from db_stats import graph_stats

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

FIELDS_STRING = ["TTLs"]
FIELDS_INTEGER = ["id.orig_p","id.resp_p","orig_bytes","resp_bytes","missed_bytes","orig_pkts","orig_ip_bytes","resp_pkts","resp_ip_bytes","qclass","qtype","trans_id","rcode","Z","depth","seen_bytes","total_bytes","missing_bytes","file_size","reply_code","data_channel.resp_p","trans_depth","request_body_len","response_body_len","status_code","info_code","dcc_file_size"]
FIELDS_FLOAT = ["duration","lease_type"]

# Output date format for timestamps
DATE_FMT="%FT%H:%M:%SZ"

BRO_CUT_CMD=["bro-cut","-U",DATE_FMT]

def unique_id(size=17):
    return ''.join(random.choice(string.ascii_lowercase + string.ascii_uppercase + string.digits) for _ in range(size))

def is_IP(s):
    # this is pretty dumb.  If it looks like an IPv4 address, fine.  But a
    # good IPv6 regex is ridiculously complex.  I took a shortcut, since I
    # this routine is only ever called to disambiguate IPs from hostnames or
    # FQDNs.  If there's even a single ":", we'll just assume this must be
    # IPv6, since neither hostnames nor FQDNs can contain that char.
    #
    # Sorry.
    return( re.match("\d+.\d+.\d+.\d+$", s) != None or re.search(":",s) != None)


def extend_list(lst, val, length):
    '''
    Given a list "lst", extend it to length "length".  Each new item will
    be composed of the value "val".  Of course, if "lst" is already "length"
    size or longer, just return and do nothing.
    '''
    if len(lst) >= length:
        return lst
    else:
        lst.extend([val] * (length - len(lst)))
        return lst

def parse_options() :
    parser = OptionParser()
    parser.add_option("-l", "--log-dir", dest="logdir",
                      help="Bro log file directory to parse.")
    parser.add_option("-q", "--quiet", dest="quiet",
                      help="Suppress unecessary output (run quietly)")
    parser.add_option("-o", "--output", dest="outputdir",default=".",
                      help="Output directory (will be created if necessary)")
    parser.add_option("-s", "--sample", dest="sample",default=False,type="int",
                      help="Randomly select SAMPLE # of connections and associated log entries.")

    (options, args) = parser.parse_args()
    return(options, args)

def readlog(file, connection_ids=False):

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

    # If this is the connection log, and if we've requested a random sample,
    # cut the dataframe down to ONLY contain that random sample
    if logtype == "conn.log" and options.sample:
        print "Size before sampling: %d" % len(df.index)
        df = df.sample(n=options.sample)
        df.reset_index(drop=True, inplace=True)
        print "Size after sampling: %d" % len(df.index)
    elif logtype == "files.log" and connection_ids:
        df = df[df.conn_uids.isin(connection_ids)]
        df.reset_index(drop=True, inplace=True)
    elif logtype != "conn.log" and connection_ids and "uid" in df.columns:
        # If this is any other type of log AND we have an explicit list of
        # connection IDs we sampled AND this is a file that has the "uid"
        # data to pair it with the conn.log, pare down the dataframe to
        # only include those rows with the right uids
        df = df[df.uid.isin(connection_ids)]
        df.reset_index(drop=True, inplace=True)
        # It is entirely possible that this sampling may mean that some
        # log files no longer have any output (for example, you only sampled
        # a list of connections, none of which were DHCP).  

    
    df.replace(to_replace=["(empty)","-"], value=["",""], inplace=True)

    # Some columns need to be forced into type String, primarily because they
    # may contain lists and we always call split() on them, but they look like
    # integers, so numpy tries to store them that way.
    for field in FIELDS_STRING:
        if field in df.columns:
            df[field] = df[field].astype(str)

    # Likewise, many rows need to be stored as Integers, but numpy thinks
    # they may be strings (probably because a legal value is "-").  This is
    # the list of the fields we know need to be converted
    for field in FIELDS_INTEGER:
        if field in df.columns:
            df[field] = df[field].replace("",-1)
            df[field] = df[field].astype(int)

    # Finally, convert the Float fields
    for field in FIELDS_FLOAT:
        if field in df.columns:
            df[field] = df[field].replace("",numpy.nan)
            df[field] = df[field].astype(float)

    if logtype == "conn.log":
        # if we're processing the conn.log AND we've requested random samples,
        # create a list of the sampled connection IDs and update the 
        # connection_ids parameter.  Otherwise, leave it the same.
        if options.sample:
            for id in df["uid"].tolist():
                connection_ids.append(id)
    
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

        # Make a direct link between the src and dest hosts, as this
        # is a common analysis task.  It doesn't *always* make sense
        # to go through the flows.
        neighbors = src_host.outV("connectedTo")
        if neighbors == None or not (dst_host in neighbors):
            e = g.connectedTo.create(src_host, dst_host)
            e.weight=1
            e.save()
        else:
            edges = edge_list(g, src_host._id, dst_host._id, "connectedTo")
            # There should only be one of these edges, and we already know
            # it exists, so it's safe to just take the first one
            edge = edges.next()
            g.connectedTo.update(edge._id, weight=(edge.weight + 1))

def graph_dns(g, df_dns):
    # Iterate through all the flows
    for i in df_dns.index:
        # Create the DNSTransaction node
        # name = str(df_dns.loc[i]["trans_id"])
        name = "%d - %s - %s" % (df_dns.loc[i]["trans_id"],
                                 df_dns.loc[i]["qtype_name"],
                                 df_dns.loc[i]["query"])
        timestamp = df_dns.loc[i]["ts"]
        flowname = df_dns.loc[i]["uid"]
        
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
            # to always be the same length, but maybe sometimes they are
            # not.  We'll force the issue by extending the TTL list to be
            # the same size as the address list.
            if df_dns.loc[i]["answers"]:
                addrs = df_dns.loc[i]["answers"].split(",")
                ttls = df_dns.loc[i]["TTLs"].split(",")
                ttls = extend_list(ttls, ttls[len(ttls)-1],len(addrs))

                for i in range(len(addrs)):
                    ans = addrs[i]
                    ttl = float(ttls[i])
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
            # print "ERROR: Flow '%s' does not exist" % flowname
            pass
        else:
            # lookup returns a generator, but since there should only be one
            # flow with this name, just take the first one
            flow = flows.next()
            nodes = flow.outV("contains")
            if nodes == None or not (transaction in nodes):
                edge = g.contains.create(flow, transaction)


        # Associate the src host with the FQDN it resolved.  Since a host
        # can resolve a domain multiple times, we'll also keep track of a
        # "weight" feature to count how many times this happened.
        if df_dns.loc[i]["query"]:
            neighbors = src.outV("resolved")
            if neighbors == None or not (fqdn in neighbors):
                e = g.resolved.create(src, fqdn)
                e.weight=1
                e.save()
            else:
                edges = edge_list(g, src._id, fqdn._id, "resolved")
                # There should only be one of these edges, and we already know
                # it exists, so it's safe to just take the first one
                edge = edges.next()
                g.resolved.update(edge._id, weight=(edge.weight + 1))
        
def graph_files(g, df_files):
    # Iterate through all the flows
    for i in df_files.index:
        # Create the file node
        name = str(df_files.loc[i]["fuid"])
        timestamp = df_files.loc[i]["ts"]
        flows = df_files.loc[i]["conn_uids"]
        
        # Create the file object. Note that this is more like a file transfer
        # transaction than a static object just for that file.  There can be
        # more than one node with the same MD5 hash, for example.  Cleary,
        # those are the same file in the real world, but not in our graph.
        #
        # However, it is possible to actually have the same file transaction
        # show up in the Bro logs multiple times.  AFAICT, this is mostly
        # due to things like timeouts, where Bro records the file transfer
        # start and then sends another log later that says that the xfer
        # failed.  We need to make sure we always check to make sure there
        # is only one File node for each actual transaction, but we'll use
        # the fields from the most recent log, assuming things that Bro
        # logs last will be more accurate.
        fileobj = g.file.get_or_create("name", name, {"name":name})

        fileobj.fuid=df_files.loc[i]["fuid"]
        fileobj.source=df_files.loc[i]["source"]
        fileobj.depth=df_files.loc[i]["depth"]
        fileobj.analyzers=df_files.loc[i]["analyzers"]
        fileobj.mime_type=df_files.loc[i]["mime_type"]
        fileobj.filename=df_files.loc[i]["filename"]
        fileobj.duration=df_files.loc[i]["duration"]
        fileobj.seen_bytes=df_files.loc[i]["seen_bytes"]
        fileobj.total_bytes=df_files.loc[i]["total_bytes"]
        fileobj.missing_bytes=df_files.loc[i]["missing_bytes"]
        fileobj.overflow_bytes=df_files.loc[i]["overflow_bytes"]
        fileobj.timedout=df_files.loc[i]["timedout"]
        fileobj.md5=df_files.loc[i]["md5"]
        fileobj.sha1=df_files.loc[i]["sha1"]
        fileobj.sha256=df_files.loc[i]["sha256"]
        fileobj.extracted=df_files.loc[i]["extracted"]
        fileobj.save()
        
        # Now connect this to the flow(s) it is associated with.
        for f in flows.split(","):
            flow = g.flow.get_or_create("name", f, {"name":f})
            g.contains.create(flow, fileobj)

        # Connect it to the src and dest hosts in the file xfer.  Note that
        # there can be more than one host listed for each side of the
        # xfer (don't ask me how).  
        for h in df_files.loc[i]["tx_hosts"].split(","):
            src = g.host.get_or_create("name", h,
                                       {"name":h,
                                        "address":h})
            g.sentBy.create(fileobj,src,{"ts":timestamp,
                                           "is_orig":df_files.loc[i]["is_orig"]})
            # Also have this extra bit of info about whether the originating
            # host is part of a local subnet.  We should make sure that is
            # recorded on the host object.
            src.local = df_files.loc[i]["local_orig"]
            src.save()
            
        for h in df_files.loc[i]["rx_hosts"].split(","):
            dst = g.host.get_or_create("name", h,
                                       {"name":h,
                                        "address":h})
            g.sentTo.create(dst, fileobj,{"ts":timestamp})
            
def graph_http(g, df_http):
    # Iterate through all the flows
    for i in df_http.index:
        # Create the HTTPTransaction node
        http = g.httpTransaction.create(name="H" + unique_id(),
                                        ts=df_http.loc[i]["ts"],
                                        resp_p=df_http.loc[i]["id.resp_p"],
                                        trans_depth=df_http.loc[i]["trans_depth"],
                                        method=df_http.loc[i]["method"].upper(),
                                        request_body_len=df_http.loc[i]["request_body_len"],
                                        response_body_len=df_http.loc[i]["response_body_len"],
                                        status_code=df_http.loc[i]["status_code"],
                                        status_msg=df_http.loc[i]["status_msg"],
                                        info_code=df_http.loc[i]["info_code"],
                                        info_msg=df_http.loc[i]["info_msg"],
                                        filename=df_http.loc[i]["filename"],
                                        tags=df_http.loc[i]["tags"],
                                        proxied=df_http.loc[i]["proxied"])
        
        # Now connect this to the flow it's associated with
        flowname = df_http.loc[i]["uid"]
        flow = g.flow.get_or_create("name", flowname, {"name":flowname})
        g.contains.create(flow, http)

        # Now connect it to the hosts on each side of the transaction
        src_addr = df_http.loc[i]["id.orig_h"]
        dst_addr = df_http.loc[i]["id.resp_h"]

        src_host = g.host.get_or_create("name", src_addr, {"name":src_addr})
        dst_host = g.host.get_or_create("name", dst_addr, {"name":dst_addr})

        g.requestedBy.create(src_host, http)
        g.requestedOf.create(http, dst_host)

        # Connect to the server host.  This can be either a domain name or
        # an IP address.  If it's a domain, we need to attach to an FQDN node.
        # If it's an IP, we need a Host node.
        h = df_http.loc[i]["host"]
        if is_IP(h):
            host = g.host.get_or_create("name", h, {"name":h})
        else:
            host = g.fqdn.get_or_create("name", h, {"name":h})

        g.hostedBy.create(http, host)

        # Now create and link to a URI node for the requested resource
        u = df_http.loc[i]["uri"]
        uri = g.uri.get_or_create("name", u, {"name":u})
        g.identifiedBy.create(http, uri)

        # Link to the UserAgent node
        ua = df_http.loc[i]["user_agent"]
        user_agent = g.userAgent.get_or_create("name", ua, {"name":ua})

        # Link to the HTTP transaction
        g.agent.create(http, user_agent)
        # Link to the host that sent the request 
        g.agent.create(src_host, user_agent)

        # Now link to the File objects transferred by this transaction.
        # Each file object also has an associated MIME type.  These are
        # encoded as two sets of paired lists:  orig_fuids/orig_mime_types
        # and resp_fuids/resp_mime_types.  In the event that the fuid list
        # is longer than the MIME type list (indicating that the last values
        # in the fuid list all have the same MIME type), we will extend the
        # mime type list to explicitly name all the mime types. It makes it
        # simpler to process the paired lists if we know they are the same
        # size.
        orig_fuids = df_http.loc[i]["orig_fuids"].split(",")
        orig_mime_types = df_http.loc[i]["orig_mime_types"].split(",")
        orig_mime_types = extend_list(orig_mime_types,
                                      orig_mime_types[len(orig_mime_types)-1],
                                      len(orig_fuids))

        resp_fuids = df_http.loc[i]["resp_fuids"].split(",")
        resp_mime_types = df_http.loc[i]["resp_mime_types"].split(",")
        resp_mime_types = extend_list(resp_mime_types,
                                      resp_mime_types[len(resp_mime_types)-1],
                                      len(resp_fuids))

        if orig_fuids != ['']:
            for x in range(len(orig_fuids)):
                fuid = orig_fuids[x]
                mime_type = orig_mime_types[x]

                f = g.file.get_or_create("name", fuid, {"name":fuid})
                g.sent.create(http, f, {"mime_type": mime_type})

        if resp_fuids != ['']:
            for x in range(len(resp_fuids)):
                try:
                    fuid = resp_fuids[x]
                    mime_type = resp_mime_types[x]
                    f = g.file.get_or_create("name", fuid, {"name":fuid})
                    g.received.create(http, f, {"mime_type": mime_type})
                except Exception, e:
                    print "****"
                    print "Exception: %s" % e
                    print
                    print resp_fuids
                    print
                    print "x: %s fuid: %s" % (x, fuid)
                    sys.exit(-1)
                    
        # Create the user account object and relationship
        username = df_http.loc[i]["username"]
        password = df_http.loc[i]["password"]
        if username:
            account = g.account.get_or_create("name", username, {"name":username})
            g.requested.create(account, http, {"password":password})
            g.uses.create(account, src_host)
        
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

connection_ids = list()

print "Graphing Flows..."
df_conn = readlog("conn.log", connection_ids)
print "Number of events: %d" % len(df_conn.index)
graph_flows(g, df_conn)

print "Graphing Files..."
df_files = readlog("files.log", connection_ids)
print "Number of events: %d" % len(df_files.index)
graph_files(g, df_files)

print "Graphing DNS Transactions..."
df_dns = readlog("dns.log", connection_ids)
print "Number of events: %d" % len(df_dns.index)
graph_dns(g, df_dns)

print "Graphing HTTP Transactions..."
df_http = readlog("http.log", connection_ids)
print "Number of events: %d" % len(df_http.index)
graph_http(g, df_http)

# Print some basic info about the graph so we know we did some real work
graph_stats(g)


    



