#!/usr/bin/python
#-*-coding:utf-8 -*-
#Author   : Xuanli He
#Version  : 1.0
#Filename : models.py

from __future__ import print_function
from collections import defaultdict

import copy
import math

from functools import reduce

import torch
from torch import nn
from torch.nn import functional as F
from transformers import AutoModel


def sequence_mask(lengths, max_len=None):
    """
    Creates a boolean mask from sequence lengths.
    """
    batch_size = lengths.numel()
    max_len = max_len or lengths.max()
    return (torch.arange(0, max_len, device=lengths.device)
            .type_as(lengths)
            .repeat(batch_size, 1)
            .lt(lengths.unsqueeze(1)))

def get_full_attention_mask(input_ids):
    return input_ids != 0

    
def get_attention_mask(adj_masks, node_pos, input_ids, text_len):
    bsz, seq_len = input_ids.shape
    attention_mask = torch.zeros(bsz, seq_len, seq_len).to(adj_masks)
    attention_mask[:, :text_len, :] = True # text may attend to everything
    attention_mask[:, :, :text_len] = True # everything may attend to text
    attention_mask[:, torch.arange(seq_len), torch.arange(seq_len)] = True # everything may attend to itself

    pad_idcs = torch.where(input_ids == 0)
    for idx_b, idx_pad in zip(*pad_idcs): # nothing may attend to or from pad
        attention_mask[idx_b, idx_pad, :] = False
        attention_mask[idx_b, :, idx_pad] = False

    for idx_b, idx_u, idx_v in zip(*torch.where(adj_masks)):
        node_pos_b = node_pos[idx_b]
        if idx_u >= len(node_pos_b) or idx_v >= len(node_pos_b):
            continue

        start_u, end_u = node_pos_b[idx_u]
        start_v, end_v = node_pos_b[idx_v]
        
        start_u += text_len
        end_u += text_len
        start_v += text_len
        end_v += text_len
        attention_mask[idx_b, start_u:end_u, start_v:end_v] = True
    
    return attention_mask



def clones(module, N):
    "Produce N identical layers."
    return nn.ModuleList([copy.deepcopy(module) for _ in range(N)])


def gating(linear, keys, query):
    query = query.unsqueeze(dim=1)
    gates = torch.sigmoid((query * keys).sum(dim=-1))

    return gates.unsqueeze(dim=-1) * keys


class GraphTrans(nn.Module):
    def __init__(self, args, node_dict, edge_dict, text_dict):
        """
        Model of graph modification

        args: args from command line
        node_dict: dictionary of nodes
        edge_dict: dictionary of edges
        text_dict: dictionary of queries
        """
        super().__init__()
        self.args = args
        self.node_dict = node_dict
        self.edge_dict = edge_dict
        self.text_dict = text_dict

        # encoder
        self.bert = AutoModel.from_pretrained("microsoft/BiomedNLP-PubMedBERT-base-uncased-abstract-fulltext")
        self.node_embeds = Embeddings(self.args.encoder_embed_dim, len(node_dict))
        self.edge_embeds = self.node_embeds

        # graph decoder
        self.graph_dec = Decoder(args, node_dict, edge_dict, self.node_embeds)

        # loss
        self.node_xent = nn.CrossEntropyLoss(reduction="sum", ignore_index=node_dict.pad())
        self.edge_xent = nn.CrossEntropyLoss(reduction="sum", ignore_index=edge_dict.pad())

    def compute_loss(self, nodes, node_outputs, edges, edge_outputs):
        """
        Calculate loss

        nodes: ground truths of target nodes: [bsz, len]
        node_outputs: generated target nodes: [bsz, len]

        edges: ground truths of target nodes: [bsz, len]
        edge_outputs: generated target nodes: [bsz, len]
        """

        # Loss over target nodes
        bsz, tgt_len = nodes.size()
        nodes = nodes.contiguous().view(bsz*tgt_len)
        node_outputs = node_outputs.view(bsz*tgt_len, -1)
        node_loss = self.node_xent(node_outputs, nodes)

        # Loss over target edge
        num_edge = edges.size(-1)
        edges = edges.contiguous().view(bsz*num_edge)
        edge_outputs = edge_outputs.view(bsz*num_edge, -1)
        edge_loss = self.edge_xent(edge_outputs, edges)

        sum_loss = node_loss + edge_loss

        return sum_loss / bsz

    def encoder(self, src_graph, src_text):
        """
        Graph encoder and query encode

        src_graph: source graphs: {nodes: [bsz, node_size], edges: [bsz, edge_size]}
        src_text: modification queries: {x: [bsz, query_size]}
        """
        # graph embed
        edge_embed = self.edge_embeds(src_graph["edges"])
        edge_masks = (src_graph["edges"] != self.edge_dict.pad()) * (src_graph["edges"] != self.edge_dict.index("<blank>"))

        adj_masks = edge_masks.clone()
        diag = torch.arange(adj_masks.size(-1))
        adj_masks[:, diag, diag] = 1

        edge_embed *= edge_masks.unsqueeze(-1)
        # graph_embed = node_embed + edge_embed.sum(dim=2)
        # src_node_masks = src_graph["nodes"] != self.node_dict.pad()

        # text embed
        # text_embed = self.position(self.text_embeds(src_text["x"]))
        # src_text_mask = src_text["x"] != self.text_dict.pad()

        text_and_graph_encodings = {}
        text_len = None
        for k, v_text in src_text.items():
            v_graph = src_graph["node_encodings"][k]
            v = torch.cat([v_text, v_graph], dim=1)
            text_len = v_text.size(1)
            text_and_graph_encodings[k] = v

        text_and_graph_encodings["token_type_ids"][:, text_len:] = 1
        text_and_graph_encodings["attention_mask"] = get_attention_mask(adj_masks=adj_masks, node_pos=src_graph["node_pos"],
                                                                        input_ids=text_and_graph_encodings["input_ids"],
                                                                        text_len=text_len)
        enc_repr = self.bert(input_ids=text_and_graph_encodings["input_ids"],
            attention_mask=text_and_graph_encodings["attention_mask"],
            token_type_ids=text_and_graph_encodings["token_type_ids"]
        )
        mem_masks = text_and_graph_encodings["input_ids"] != 0

        enc_info = {"mem": enc_repr["last_hidden_state"], "mem_masks": mem_masks}

        return enc_info

    def forward(self, src_graph, src_text, tgt_graph):
        
        # graph encoder
        enc_info = self.encoder(src_graph, src_text)
        # graph decoder
        _, node_outputs, _, edge_outputs = self.graph_dec(enc_info, tgt_graph["nodes"], tgt_graph["edges"]) 

        return self.compute_loss(tgt_graph["nodes"]["y"], node_outputs, tgt_graph["edges"]["y"], edge_outputs)


# Modified from http://nlp.seas.harvard.edu/2018/04/03/attention.html
class Encoder(nn.Module):
    "Core encoder is a stack of N layers"
    def __init__(self, layer, N):
        super().__init__()
        self.layers = clones(layer, N)
        self.norm = LayerNorm(layer.size)
        
    def forward(self, x, mask):
        "Pass the input (and mask) through each layer in turn."
        for layer in self.layers:
            x = layer(x, mask)
        return self.norm(x)


# Modified from http://nlp.seas.harvard.edu/2018/04/03/attention.html
class LayerNorm(nn.Module):
    "Construct a layernorm module (See citation for details)."
    def __init__(self, features, eps=1e-6):
        super().__init__()
        self.a_2 = nn.Parameter(torch.ones(features))
        self.b_2 = nn.Parameter(torch.zeros(features))
        self.eps = eps

    def forward(self, x):
        mean = x.mean(-1, keepdim=True)
        std = x.std(-1, keepdim=True)
        return self.a_2 * (x - mean) / (std + self.eps) + self.b_2


# Modified from http://nlp.seas.harvard.edu/2018/04/03/attention.html
class SublayerConnection(nn.Module):
    """
    A residual connection followed by a layer norm.
    Note for code simplicity the norm is first as opposed to last.
    """
    def __init__(self, size, dropout):
        super().__init__()
        self.norm = LayerNorm(size)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x, sublayer):
        "Apply residual connection to any sublayer with the same size."
        return x + self.dropout(sublayer(self.norm(x)))


# Modified from http://nlp.seas.harvard.edu/2018/04/03/attention.html
class EncoderLayer(nn.Module):
    "Encoder is made up of self-attn and feed forward (defined below)"
    def __init__(self, size, self_attn, feed_forward, dropout):
        super().__init__()
        self.self_attn = self_attn
        self.feed_forward = feed_forward
        self.sublayer = clones(SublayerConnection(size, dropout), 2)
        self.size = size

    def forward(self, x, mask):
        "Follow Figure 1 (left) for connections."
        x = self.sublayer[0](x, lambda x: self.self_attn(x, x, x, mask))
        return self.sublayer[1](x, self.feed_forward)


# Modified from http://nlp.seas.harvard.edu/2018/04/03/attention.html
def attention(query, key, value, mask=None, dropout=None):
    "Compute 'Scaled Dot Product Attention'"
    d_k = query.size(-1)
    scores = torch.matmul(query, key.transpose(-2, -1)) \
             / math.sqrt(d_k)
    if mask is not None:
        scores = scores.masked_fill(mask == 0, -1e9)
    p_attn = F.softmax(scores, dim = -1)
    if dropout is not None:
        p_attn = dropout(p_attn)
    return torch.matmul(p_attn, value), p_attn


# Modified from http://nlp.seas.harvard.edu/2018/04/03/attention.html
class MultiHeadedAttention(nn.Module):
    def __init__(self, h, d_model, dropout=0.1):
        "Take in model size and number of heads."
        super().__init__()
        assert d_model % h == 0
        # We assume d_v always equals d_k
        self.d_k = d_model // h
        self.h = h
        self.linears = clones(nn.Linear(d_model, d_model), 4)
        self.attn = None
        self.dropout = nn.Dropout(p=dropout)
        
    def forward(self, query, key, value, mask=None):
        "Implements Figure 2"
        if mask is not None:
            # Same mask applied to all h heads.
            mask = mask.unsqueeze(1)
        nbatches = query.size(0)
        
        # 1) Do all the linear projections in batch from d_model => h x d_k 
        query, key, value = \
            [l(x).view(nbatches, -1, self.h, self.d_k).transpose(1, 2)
             for l, x in zip(self.linears, (query, key, value))]
        
        # 2) Apply attention on all the projected vectors in batch. 
        x, self.attn = attention(query, key, value, mask=mask, 
                                 dropout=self.dropout)
        
        # 3) "Concat" using a view and apply a final linear. 
        x = x.transpose(1, 2).contiguous() \
             .view(nbatches, -1, self.h * self.d_k)
        return self.linears[-1](x)


# Modified from http://nlp.seas.harvard.edu/2018/04/03/attention.html
class PositionwiseFeedForward(nn.Module):
    "Implements FFN equation."
    def __init__(self, d_model, d_ff, dropout=0.1):
        super().__init__()
        self.w_1 = nn.Linear(d_model, d_ff)
        self.w_2 = nn.Linear(d_ff, d_model)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        return self.w_2(self.dropout(F.relu(self.w_1(x))))


# Modified from http://nlp.seas.harvard.edu/2018/04/03/attention.html
class Embeddings(nn.Module):
    def __init__(self, d_model, vocab):
        super().__init__()
        self.lut = nn.Embedding(vocab, d_model)
        self.d_model = d_model

    def forward(self, x):
        return self.lut(x) * math.sqrt(self.d_model)


# Modified from http://nlp.seas.harvard.edu/2018/04/03/attention.html
class PositionalEncoding(nn.Module):
    "Implement the PE function."
    def __init__(self, d_model, dropout, max_len=5000):
        super().__init__()
        self.dropout = nn.Dropout(p=dropout)
        
        # Compute the positional encodings once in log space.
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len).unsqueeze(1).to(pe.dtype)
        div_term = torch.exp(torch.arange(0, d_model, 2).to(pe.dtype) *
                             -(math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0)
        self.register_buffer('pe', pe)
        
    def forward(self, x):
        x = x + self.pe[:, :x.size(1)]

        return self.dropout(x)


class Attention(nn.Module):
    """
    Luong's attention
    """
    def __init__(self, input_size, mem_size):
        super().__init__()
        self.dim = mem_size
        self.linear_q = nn.Linear(input_size, mem_size, bias=False)
        self.linear_c = nn.Linear(mem_size, mem_size, bias=True)
        self.v = nn.Linear(mem_size, 1, bias=False)
        self.linear_out = nn.Linear(mem_size+input_size, input_size, bias=True)

    def score(self, q, m):
        src_batch, src_len, src_dim = m.size()
        tgt_batch, tgt_len, tgt_dim = q.size() 

        bsz = src_batch
        dim = self.dim

        wq = self.linear_q(q).view(bsz, tgt_len, 1, dim).expand(bsz, tgt_len, src_len, dim)
        uh = self.linear_c(m).view(bsz, 1, src_len, dim).expand(bsz, tgt_len, src_len, dim)

        wquh = torch.tanh(wq + uh)

        return self.v(wquh).view(bsz, tgt_len, src_len)

    def forward(self, inputs, mems, mem_masks=None):
        
        align = self.score(inputs, mems)

        if mem_masks is not None:
            if mem_masks.dim() == 1:
                mask = sequence_mask(mem_masks, max_len=align.size(-1))
            else:
                mask = mem_masks
            mask = mask.unsqueeze(1)  # Make it broadcastable.
            align.masked_fill_(~mask, -float('inf'))

        align_vectors = F.softmax(align, -1)

        c = torch.bmm(align_vectors, mems)

        attn_h = self.linear_out(torch.cat([c, inputs], -1))
        
        return attn_h, align_vectors


class Decoder(nn.Module):
    """
    Generate nodes and relations
    """
    def __init__(self, args, node_dict, edge_dict, embeds):
        super().__init__()
        self.args = args
        self.pad_idx = node_dict.pad()

        self.dropout = nn.Dropout(args.dropout)
        
        # node-level generation
        node_types = len(node_dict)
        # self.node_embeds = nn.Embedding(node_types, args.node_embed_size, padding_idx=node_dict.pad())
        self.node_embeds = embeds
        self.node_RNN = nn.GRU(args.node_embed_size, args.node_hidden_size, batch_first=True,
                               num_layers=args.dec_layers, dropout=args.dropout)
        self.node_att = Attention(args.node_hidden_size, args.encoder_embed_dim)
        # self.node_out_proj = nn.Linear(args.node_hidden_size, node_types)
        if args.node_embed_size != args.encoder_embed_dim:
            self.node_input_proj = nn.Linear(args.encoder_embed_dim, args.node_embed_size)
        else:
            self.node_input_proj = None
        if args.node_hidden_size != args.encoder_embed_dim:
            self.node_out_proj = nn.Linear(args.node_hidden_size, args.encoder_embed_dim)
        else:
            self.node_out_proj = None

        # edge-level generation
        edge_types = len(edge_dict)
        # self.edge_embeds = nn.Embedding(edge_types, args.edge_embed_size, padding_idx=edge_dict.pad())
        self.edge_embeds = embeds
        self.edge_RNN = nn.GRU(args.edge_embed_size+args.node_hidden_size*2, args.edge_hidden_size, batch_first=True,
                               num_layers=args.dec_layers, dropout=args.dropout)
        self.edge_att = Attention(args.edge_hidden_size, args.encoder_embed_dim)
        # self.edge_out_proj = nn.Linear(args.edge_hidden_size, edge_types)
        if args.edge_embed_size != args.encoder_embed_dim:
            self.edge_input_proj = nn.Linear(args.encoder_embed_dim, args.edge_embed_size)
        else:
            self.edge_input_proj = None
        if args.edge_hidden_size != args.encoder_embed_dim:
            self.edge_out_proj = nn.Linear(args.edge_hidden_size, args.encoder_embed_dim)
        else:
            self.edge_out_proj = None

    def node_forward(self, enc_info, nodes, nodes_len, init_hiddens=None):
        """node-level generation

        enc_info: hidden states of source graphs and source queries
        nodes: ground truths of target nodes: [bsz, len]
        nodes_len: masks for target nodes: [bsz, len]
        init_hiddens: initial hidden states for RNN
        """
        bsz, steps = nodes.size()
        nodes_embeds = self.node_embeds(nodes)
        if self.node_input_proj:
            nodes_embeds = self.node_input_proj(nodes_embeds)

        padded_nodes_embeds = nn.utils.rnn.pack_padded_sequence(nodes_embeds, nodes_len.cpu(), batch_first=True)
        rnn_packed_outputs, h = self.node_RNN(padded_nodes_embeds, init_hiddens)

        rnn_outputs = nn.utils.rnn.pad_packed_sequence(rnn_packed_outputs, batch_first=True)[0]

        context, _ = self.node_att(rnn_outputs, enc_info["mem"], enc_info["mem_masks"])
        # outputs = self.node_out_proj(self.dropout(context))
        if self.node_out_proj:
            outputs = self.node_out_proj(context) 
        else:
            outputs = context
        outputs = F.linear(self.dropout(outputs), self.node_embeds.lut.weight)

        return rnn_outputs, h, outputs

    def edge_forward(self, enc_info, edges, src_nodes, tgt_nodes, init_hiddens=None):
        """
        Edge-level decoder

        enc_info: hidden states of source graphs and source queries
        edges: ground truths of edges of target graphs: [bsz, len]
        src_nodes: source nodes of edges of target graphs   *src node* <-edge-> tgt node
        tgt_nodes: target nodes of edges of target graphs    src node <-edge-> *tgt node*
        init_hiddens: initial hidden states for RNN
        """

        # embedding
        edges_embeds = self.edge_embeds(edges)
        if self.edge_input_proj:
            edges_embeds = self.edge_input_proj(edges_embeds)

        # rnn
        rnn_inputs = torch.cat([edges_embeds, src_nodes, tgt_nodes], dim=-1)

        rnn_outputs, h = self.edge_RNN(rnn_inputs, init_hiddens)

        context, _ = self.edge_att(rnn_outputs, enc_info["mem"], enc_info["mem_masks"])
        # outputs = self.edge_out_proj(self.dropout(context))
        if self.edge_out_proj:
            outputs = self.edge_out_proj(context) 
        else:
            outputs = context
        outputs = F.linear(self.dropout(outputs), self.edge_embeds.lut.weight)

        return rnn_outputs, h, outputs

    def forward(self, enc_info, nodes, edges):
        """
        Graph generator
        """
        # node-level decoder
        nodes_lens = (nodes["x"] != self.pad_idx).long().sum(dim=-1)
        node_rnn_outputs, _, node_outputs = self.node_forward(enc_info, nodes["x"], nodes_lens)

        node_size = node_rnn_outputs.size(1)
        # build indices of source nodes of edges of target graphs
        src_nodes_indices = reduce(lambda x, y: x+y, [[i for _ in range(i)] for i in range(1, node_size-1)]) if node_size > 2 else []
        src_nodes_indices = src_nodes_indices + [src_nodes_indices[-1]+1] if src_nodes_indices else [0]
        src_nodes_indices = torch.tensor(src_nodes_indices).to(node_rnn_outputs.device)
        src_nodes = torch.index_select(node_rnn_outputs, 1, src_nodes_indices)

        # build indices of target nodes of edges of target graphs
        tgt_nodes_indices = reduce(lambda x, y: x+y, [[j for j in range(i)] for i in range(1, node_size-1)]) if node_size > 2 else []
        tgt_nodes_indices = tgt_nodes_indices + [src_nodes_indices[-1]] if tgt_nodes_indices else [0]
        tgt_nodes_indices = torch.tensor(tgt_nodes_indices).to(node_rnn_outputs.device)
        tgt_nodes = torch.index_select(node_rnn_outputs, 1, tgt_nodes_indices)

        # edge-level decoder
        edge_rnn_outputs, _, edge_outputs = self.edge_forward(enc_info, edges["x"], src_nodes, tgt_nodes)

        return node_rnn_outputs, node_outputs, edge_rnn_outputs, edge_outputs
