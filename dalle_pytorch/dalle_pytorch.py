from math import sqrt
import torch
from torch import nn, einsum
import torch.nn.functional as F

from einops import rearrange
from x_transformers import Encoder, Decoder

# helpers

def exists(val):
    return val is not None

def is_empty(t):
    return t.nelement() == 0

def masked_mean(t, mask, dim = 1):
    t = t.masked_fill(~mask[:, :, None], 0.)
    return t.sum(dim = 1) / mask.sum(dim = 1)[..., None]

def eval_decorator(fn):
    def inner(model, *args, **kwargs):
        was_training = model.training
        model.eval()
        out = fn(model, *args, **kwargs)
        model.train(was_training)
        return out
    return inner

# sampling helpers

def top_k(logits, thres = 0.5):
    num_logits = logits.shape[-1]
    k = max(int((1 - thres) * num_logits), 1)
    val, ind = torch.topk(logits, k)
    probs = torch.full_like(logits, float('-inf'))
    probs.scatter_(1, ind, val)
    return probs

# discrete vae class

class DiscreteVAE(nn.Module):
    def __init__(
        self,
        num_tokens = 512,
        dim = 512,
        hidden_dim = 64,
        num_layers = 3,
        channels = 3
    ):
        super().__init__()
        hdim = hidden_dim
        
        assert num_layers >= 1
        
        encoder_layers = []
        decoder_layers = []
        for i in range(num_layers):
            enc_in = channels if i == 0 else hdim
            encoder_layers += [
                nn.Conv2d(enc_in, hdim, 4, stride = 2, padding = 1),
                nn.ReLU(),
            ]
            
            dec_in = dim if i == 0 else hdim
            decoder_layers += [
                nn.ConvTranspose2d(dec_in, hdim, 4, stride = 2, padding = 1),
                nn.ReLU(),
            ]
            
        encoder_layers.append(nn.Conv2d(hdim, num_tokens, 1))
        decoder_layers.append(nn.Conv2d(hdim, channels, 1))

        self.encoder = nn.Sequential(*encoder_layers)
        self.decoder = nn.Sequential(*decoder_layers)

        self.num_tokens = num_tokens
        self.codebook = nn.Embedding(num_tokens, dim)

    @torch.no_grad()
    def get_codebook_indices(self, images):
        logits = self.forward(images, return_logits = True)
        codebook_indices = logits.argmax(dim = 1).flatten(1)
        return codebook_indices

    def decode(
        self,
        img_seq
    ):
        image_embeds = self.codebook(img_seq)
        b, n, d = image_embeds.shape
        h = w = int(sqrt(n))

        image_embeds = rearrange(image_embeds, 'b (h w) d -> b d h w', h = h, w = w)
        images = self.decoder(image_embeds)
        return images

    def forward(
        self,
        img,
        return_recon_loss = False,
        return_logits = False
    ):
        logits = self.encoder(img)

        if return_logits:
            return logits # return logits for getting hard image indices for DALL-E training

        soft_one_hot = F.gumbel_softmax(logits, tau = 1.)
        sampled = einsum('b n h w, n d -> b d h w', soft_one_hot, self.codebook.weight)
        out = self.decoder(sampled)

        if not return_recon_loss:
            return out

        loss = F.mse_loss(img, out)
        return loss

# main classes

class CLIP(nn.Module):
    def __init__(
        self,
        *,
        dim_text = 512,
        dim_image = 512,
        dim_latent = 512,
        num_text_tokens = 10000,
        text_enc_depth = 6,
        text_seq_len = 256,
        text_heads = 8,
        num_visual_tokens = 512,
        visual_enc_depth = 6,
        visual_heads = 8,
        visual_image_size = 256,
        visual_patch_size = 32,
        channels = 3,
        vae = None
    ):
        super().__init__()
        self.text_emb = nn.Embedding(num_text_tokens, dim_text)
        self.text_pos_emb = nn.Embedding(text_seq_len, dim_text)
        self.text_transformer = Encoder(dim = dim_text, depth = text_enc_depth, heads = text_heads)
        self.to_text_latent = nn.Linear(dim_text, dim_latent, bias = False)

        assert visual_image_size % visual_patch_size == 0, 'Image dimensions must be divisible by the patch size.'
        num_patches = (visual_image_size // visual_patch_size) ** 2
        patch_dim = channels * visual_patch_size ** 2

        self.visual_patch_size = visual_patch_size
        self.to_visual_embedding = nn.Linear(patch_dim, dim_image)
        self.visual_pos_emb = nn.Embedding(num_patches, dim_image)
        self.visual_transformer = Encoder(dim = dim_image, depth = visual_enc_depth, heads = visual_heads)
        self.to_visual_latent = nn.Linear(dim_image, dim_latent, bias = False)

        self.temperature = nn.Parameter(torch.tensor(1.))

        self.vae = vae
        if exists(self.vae):
            self.vae = vae
            self.visual_emb = vae.codebook

    def forward(
        self,
        text,
        image,
        text_mask = None,
        return_loss = False
    ):
        b, device, p = text.shape[0], text.device, self.visual_patch_size

        if exists(self.vae):
            image = self.vae.get_codebook_indices(image)

        text_emb = self.text_emb(text)
        text_emb += self.text_pos_emb(torch.arange(text.shape[1], device = device))

        image_patches = rearrange(image, 'b c (h p1) (w p2) -> b (h w) (p1 p2 c)', p1 = p, p2 = p)
        image_emb = self.to_visual_embedding(image_patches)
        image_emb += self.visual_pos_emb(torch.arange(image_emb.shape[1], device = device))

        enc_text = self.text_transformer(text_emb, mask = text_mask)
        enc_image = self.visual_transformer(image_emb)

        if exists(text_mask):
            text_latents = masked_mean(enc_text, text_mask, dim = 1)
        else:
            text_latents = enc_text.mean(dim = 1)

        image_latents = enc_image.mean(dim = 1)

        text_latents = self.to_text_latent(text_latents)
        image_latents = self.to_visual_latent(image_latents)

        text_latents, image_latents = map(lambda t: F.normalize(t, p = 2, dim = -1), (text_latents, image_latents))

        temp = self.temperature.exp()

        if not return_loss:
            sim = einsum('n d, n d -> n', text_latents, image_latents) * temp
            return sim

        sim = einsum('i d, j d -> i j', text_latents, image_latents) * temp
        labels = torch.arange(b, device = device)
        loss = F.cross_entropy(sim, labels)
        return loss


class DALLE(nn.Module):
    def __init__(
        self,
        *,
        dim,
        num_text_tokens = 10000,
        num_image_tokens = 512,
        text_seq_len = 256,
        image_seq_len = 1024,
        depth = 6, # should be 64
        heads = 8,
        vae = None
    ):
        super().__init__()
        self.text_emb = nn.Embedding(num_text_tokens, dim)
        self.image_emb = nn.Embedding(num_image_tokens, dim)

        self.text_pos_emb = nn.Embedding(text_seq_len, dim)
        self.image_pos_emb = nn.Embedding(image_seq_len, dim)

        self.num_text_tokens = num_text_tokens # for offsetting logits index and calculating cross entropy loss
        self.num_image_tokens = num_image_tokens
        self.text_seq_len = text_seq_len
        self.image_seq_len = image_seq_len

        seq_len = text_seq_len + image_seq_len
        total_tokens = num_text_tokens + num_image_tokens + 1 # extra for EOS
        self.total_tokens = total_tokens
        
        self.vae = vae
        if exists(self.vae):
            self.vae = vae
            self.image_emb = vae.codebook

        self.transformer = Decoder(dim = dim, depth = depth, heads = heads)

        self.to_logits = nn.Sequential(
            nn.LayerNorm(dim),
            nn.Linear(dim, self.total_tokens),
        )

        seq_range = torch.arange(seq_len)
        logits_range = torch.arange(total_tokens)

        seq_range = rearrange(seq_range, 'n -> () n ()')
        logits_range = rearrange(logits_range, 'd -> () () d')

        logits_mask = (
            ((seq_range >= (text_seq_len - 1)) & (logits_range < num_text_tokens)) |
            ((seq_range < (text_seq_len - 1)) & (logits_range >= num_text_tokens)) |
            ((seq_range != (seq_len - 1)) & (logits_range >= (total_tokens - 1)))
        )

        self.register_buffer('logits_mask', logits_mask)

    @torch.no_grad()
    @eval_decorator
    def generate_images(
        self,
        vae,
        text,
        clipper = None,
        mask = None,
        filter_thres = 0.5,
        temperature = 1.
    ):
        text_seq_len, image_seq_len, num_text_tokens = self.text_seq_len, self.image_seq_len, self.num_text_tokens
        total_len = text_seq_len + image_seq_len

        out = text
        for cur_len in range(text.shape[1], total_len):
            is_image = cur_len >= text_seq_len

            text, image = out[:, :text_seq_len], out[:, text_seq_len:]

            logits = self(text, image, mask = mask)[:, -1, :]

            filtered_logits = top_k(logits, thres = filter_thres)
            probs = F.softmax(filtered_logits / temperature, dim = -1)
            sample = torch.multinomial(probs, 1)

            sample -= (num_text_tokens if is_image else 0) # offset sampled token if it is an image token, since logit space is composed of text and then image tokens
            out = torch.cat((out, sample), dim=-1)

            if out.shape[1] <= text_seq_len:
                mask = F.pad(mask, (0, 1), value = True)

        text_seq = out[:, :text_seq_len]

        img_seq = out[:, -image_seq_len:]
        images = vae.decode(img_seq)

        if exists(clipper):
            scores = clipper(text_seq, images, return_loss = False)
            return images, scores

        return images

    def forward(
        self,
        text,
        image = None,
        mask = None,
        return_loss = False
    ):
        device = text.device
        eos_token_id = self.total_tokens - 1

        tokens = self.text_emb(text)
        tokens += self.text_pos_emb(torch.arange(text.shape[1], device = device))

        seq_len = tokens.shape[1]

        if exists(image) and not is_empty(image):
            is_raw_image = len(image.shape) == 4
            image_len = image.shape[1]
            seq_len += image_len

            if is_raw_image:
                assert exists(self.vae), 'VAE must be passed into constructor if you are to train directly on raw images'
                image = self.vae.get_codebook_indices(image)

            image_emb = self.image_emb(image)
            image_emb += self.image_pos_emb(torch.arange(image_len, device = device))

            tokens = torch.cat((tokens, image_emb), dim = 1)

            if exists(mask):
                mask = F.pad(mask, (0, image_emb.shape[1]), value = True)

        out = self.transformer(tokens, mask = mask)
        logits = self.to_logits(out)

        # mask logits to make sure text predicts text (except last token), and image predicts image
        mask = self.logits_mask[:, :seq_len]
        max_neg_value = -torch.finfo(logits.dtype).max
        logits.masked_fill_(mask, max_neg_value)

        if not return_loss:
            return logits

        assert exists(image), 'when training, image must be supplied'

        offsetted_image = image + self.num_text_tokens
        labels = torch.cat((text, offsetted_image), dim = 1)
        labels = F.pad(labels, (0, 1), value = eos_token_id) # last token predicts EOS
        loss = F.cross_entropy(logits.transpose(1, 2), labels[:, 1:])
        return loss
