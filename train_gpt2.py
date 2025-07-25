import math
from dataclasses import dataclass
import torch
import torch.nn as nn
from torch.nn import functional as F



class CausalSelfAttention(nn.Module):
    def __init__(self, config):
        super().__init__()
        assert config.n_embd % config.n_head == 0
        self.c_attn = nn.Linear(config.n_embd, 3* config.n_embd) #key, query, value projections for all heads, but in a batch
        self.c_proj = nn.Linear(config.n_embd, config.n_embd)
        self.n_head = config.n_head
        self.n_embd = config.n_embd
        
        #not really a bias, but rather mask. This is OpenAIs naming convention
        self.register_buffer("bias", torch.tril(torch.ones(config.block_size, config.block_size))
                                    .view(1, 1, config.block_size, config.block_size))

    def forward(self, x):
        B,T,C = x.size()
        qkv = self.c_attn(x)
        q, k, v = qkv.split(self.n_embd, dim=2)
        k = k.view(B,T,self.n_head, C // self.n_head).transpose(1,2) # B, nh, T, hs
        q = q.view(B,T,self.n_head, C // self.n_head).transpose(1,2) # B, nh, T, hs
        v = v.view(B,T,self.n_head, C // self.n_head).transpose(1,2) # B, nh, T, hs
        #attention (materializes the large (T,T) matrix for all the queries and keys) 
        att = (q @ k.transpose(-2, -1)) * (1.0 / math.sqrt(k.size(-1)))
        att = att.masked_fill(self.bias[:,:,:T,:T] == 0, float('-inf')) # mask makes sure that the tokens attend to the tokens in the past, not future
        att = F.softmax(att, dim=-1) # softmax normalizes attentions that their sum makes 1, dim = -1 "auto-captures" dimensions from the supported matrix 
        y = att @ v # (B, nh, T, T) x (B, nh, T, hs) -> (B, nh, T, hs) | matrix multiplication results in a weighted sum
        y = y.transpose(1, 2).contiguous().view(B,T,C) #re-assemble all head outputs side by side
        #output projection
        y = self.c_proj(y)
        return y


class MLP(nn.Module): #multilayer perceptron
    def __init__(self, config):
        super().__init__()
        self.c_fc = nn.Linear(config.n_embd, 4*config.n_embd)
        self.gelu = nn.GELU(approximate='tanh') #Gaussian Error Linear Units (GELU)
        self.c_proj = nn.Linear(4*config.n_embd, config.n_embd)

    def forward(self, x):
        x = self.c_fc(x)
        x = self.gelu(x)
        x = self.c_proj(x)
        return x


class Block(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.ln_1 = nn.LayerNorm(config.n_embd)
        self.attn = CausalSelfAttention(config)
        self.ln_2 = nn.LayerNorm(config.n_embd)
        self.mlp = MLP(config)

    def forward(self, x):
        x = x + self.attn(self.ln_1(x)) # attn is aggregation, reduce weighted sum function
        x = x + self.mlp(self.ln_2(x)) # mlp does not exchange info between tokens
        return x



@dataclass
class GPTConfig:
    block_size: int = 1024
    vocab_size: int = 50257
    n_layer: int = 12
    n_head: int = 12
    n_embd: int = 768

class GPT(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config

        self.transformer = nn.ModuleDict(dict(
            wte = nn.Embedding(config.vocab_size, config.n_embd),  # embedding is a wrapper for tensor
            wpe = nn.Embedding(config.block_size, config.n_embd),
            h = nn.ModuleList([Block(config) for _ in range(config.n_layer)]),
            ln_f = nn.LayerNorm(config.n_embd),
        ))
        self.lm_head = nn.Linear(config.n_embd, config.vocab_size, bias=False)

        #weight sharing scheme
        self.transformer.wte.weight = self.lm_head.weight

        # init params
        self.apply(_init_weights)

    def _init_weights(self, module):
        if isinstance(module. nn.Linear):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)

    def forward(self, idx, targets=None): #idx -> token indices as input
        # idx is of shape (B, T)
        B, T = idx.size()
        assert T <= self.config.block_size, f"Cannot forward sequence of length {T}, block size is only {self.config.block_size}"
        # forward the token and posisition embeddings
        pos = torch.arange(0, T, dtype=torch.long, device=idx.device) # shape (T)
        pos_emb = self.transformer.wpe(pos) # position embeddings of shape (T, n_embd)
        tok_emb = self.transformer.wte(idx) # token embeddings of shape (B, T, n_embd)
        x = tok_emb + pos_emb #B has to be artificially created for position embeddings in order to be able to sum it with token embeddings
        # forward the blocks of the transformer
        for block in self.transformer.h:
            x = block(x)
        # forward the final layernorm and the classifier
        x = self.transformer.ln_f(x)
        logits = self.lm_head(x) # (B, T, vocab_size)  this calculates B, T+1 (next token that comes after), vocab size is dimension specifiing number of possible tokens
        loss=None
        if targets is not None:
            loss = F.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1)) # flatten logits first two dimensions to just 1 (cross_entropy does not accept more than 2 dimensions)
        return logits, loss


    @classmethod
    def from_pretrained(cls, model_type):
        """Loads pretrained GPT-2 model weights from huggingface"""
        assert model_type in {'gpt2', 'gpt2-medium', 'gpt2-large', 'gpt2-xl'}
        from transformers import GPT2LMHeadModel
        print("loading weights from pretrained gpt: %s" % model_type)

        config_args = {
            'gpt2':             dict(n_layer=12, n_head=12, n_embd=768),
            'gpt2-medium':      dict(n_layer=24, n_head=16, n_embd=1024),
            'gpt2-large':       dict(n_layer=36, n_head=20, n_embd=1280),
            'gpt2-xl':          dict(n_layer=48, n_head=25, n_embd=1600)
        }[model_type]
        config_args['vocab_size'] = 50257 # always 50257 for GPT model checkpoints
        config_args['block_size'] = 1024 # always 1024 for GTPT model checkpoints
        # create a from-scratch initialized minGPT model
        config = GPTConfig(**config_args)
        model = GPT(config)
        sd = model.state_dict()
        sd_keys = sd.keys()
        sd_keys = [k for k in sd_keys if not k.endswith('.attn.bias')] # discard this mask / buffer, not a param

        # init a huggingface/transformers model
        model_hf = GPT2LMHeadModel.from_pretrained(model_type)
        sd_hf = model_hf.state_dict()

        # copy while ensuring all of the parameters are aligned and match in names and shapes
        sd_keys_hf = sd_hf.keys()
        sd_keys_hf = [k for k in sd_keys_hf if not k.endswith('.attn.masked_bias')] # ignore these
        sd_keys_hf = [k for k in sd_keys_hf if not k.endswith('.attn.bias')] # ignore these
        transposed = ['attn.c_attn.weight', 'attn.c_proj.weight', 'mlp.c_fc.weight', 'mlp.c_proj.weight']
        # basically the openai checkpoints use a "Conv1D" module, but we only want to use a vanilla Linear
        # this means that we have to transpose these weights when we import them
        assert len(sd_keys_hf) == len(sd_keys), f"mismatched keys: {len(sd_keys_hf)} != {len(sd_keys)}"
        for k in sd_keys_hf:
            if any(k.endswith(w) for w in transposed):
                #special treatment for the COnv1D weights we need to transpose
                assert sd_hf[k].shape[::-1] == sd[k].shape
                with torch.no_grad():
                    sd[k].copy_(sd_hf[k].t())
            else:
                #vanilla copy over the other parameters
                assert sd_hf[k].shape == sd[k].shape
                with torch.no_grad():
                    sd[k].copy_(sd_hf[k])

        return model





import tiktoken

class DataLoaderLite:
    def __init__(self, B, T):
        self.B = B
        self.T = T

        # at init load tokens from disk and store them in mem
        with open('input.txt', 'r') as f:
            text = f.read()
        enc = tiktoken.get_encoding('gpt2')
        tokens = enc.encode(text)
        self.tokens = torch.tensor(tokens)
        print(f"loaded {len(self.tokens)} tokens")
        print(f"1 epoch = {len(self.tokens) // (B * T)} batches")

        # state
        self.current_position = 0

    def next_batch(self):
        B, T = self.B, self.T
        buf = self.tokens[self.current_position : self.current_position+B*T+1]
        x = (buf[:-1]).view(B, T)
        y = (buf[1:]).view(B, T)
        # advance the position in the tensor
        self.current_position += B*T
        if self.current_position + (B*T+1) > len(self.tokens):
            self.current_position = 0
        return x,y








# --------------------
# auto get device
device = 'cpu'
if torch.cuda.is_available():
    device = 'cuda'
elif hasattr(torch.backends, "mps"):
    device = "mps"
print(f"using device: {device}")



train_loader = DataLoaderLite(B=4, T=32)


# get logits
model = GPT(GPTConfig())
model.to(device)

# optimize
optimizer = torch.optim.AdamW(model.parameters(), lr=3e-4)
for i in range(50):
    x,y = train_loader.next_batch()
    x,y = x.to(device), y.to(device)
    optimizer.zero_grad()
    logits, loss = model(x,y)
    loss.backward()
    optimizer.step()
    print(f"step {i}, loss: {loss.item()}")



print(loss)
import sys; sys.exit(0)







#prefix tokens
model.eval()
num_return_sequences = 5
max_length = 30
import tiktoken
enc = tiktoken.get_encoding('gpt2')
tokens = enc.encode("Hello world!")
tokens = torch.tensor(tokens, dtype=torch.long)
tokens = tokens.unsqueeze(0).repeat(num_return_sequences, 1)        # this will replicate the tokens 5 times (num_torch_sequences)
x = tokens.to(device)


# generate! right now x is (B, T) where B = 5, T = 8
torch.manual_seed(42)
torch.cuda.manual_seed(42)
while x.size(1) < max_length:
    with torch.no_grad():                                           # without backtracking gradients, saving perf
        logits = model(x)
        logits = logits[:, -1, :]                                   # take logits at last position
        probs = F.softmax(logits, dim=-1)                           # get probabilities
        topk_probs, topk_indices = torch.topk(probs, 50, dim=-1)    # top-k sampling (huggingface default) #topk_rpobs here becomes (5, 50), topk_indices is (5, 50) | 
                                                                    # this samples 50 most likely tokens, anything outside this range becomes irrelevant

        ix = torch.multinomial(topk_probs, 1)                       # (B, 1) select a token from the top-k probabilities
        xcol = torch.gather(topk_indices, -1, ix)                   # (B,1)
        x = torch.cat((x, xcol), dim=1)                             # append to the sequence


# print generated text
for i in range(num_return_sequences):
    tokens = x[i, :max_length].tolist()
    decoded = enc.decode(tokens)
    print(">", decoded)