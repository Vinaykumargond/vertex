import copy
import functools
from typing import Any, Dict

import torch
from torch import nn
from torch.nn import functional as F

from virtex.data.structures import ImageCaptionBatch
from virtex.data.tokenizers import SentencePieceBPETokenizer
from virtex.modules.textual_heads import TextualHead
from virtex.modules.visual_backbones import VisualBackbone
from virtex.utils.beam_search import AutoRegressiveBeamSearch


class CaptioningModel(nn.Module):
    r"""
    A model to perform image captioning (in both forward and backward directions
    independently, only in forward direction). It is composed of a
    :class:`~virtex.modules.visual_backbones.VisualBackbone` and a
    :class:`~virtex.modules.textual_heads.TextualHead` on top of it.

    During training, it maximizes the likelihood of ground truth caption
    conditioned on image features. During inference, it predicts a caption for
    an input image through beam search decoding.

    Parameters
    ----------
    visual: virtex.modules.visual_backbones.VisualBackbone
        A :class:`~virtex.modules.visual_backbones.VisualBackbone` which
        computes visual features from an input image.
    textual: virtex.modules.textual_heads.TextualHead
        A :class:`~virtex.modules.textual_heads.TextualHead` which
        makes final predictions conditioned on visual features.
    beam_size : int, optional (default = 5)
        The width of the beam used for beam search.
    max_decoding_steps: int, optional (default = 30)
        The maximum number of decoding steps for beam search.
    sos_index: int, optional (default = 1)
        The index of the end token (``[SOS]``) in vocabulary.
    eos_index: int, optional (default = 2)
        The index of the end token (``[EOS]``) in vocabulary.
    caption_backward: bool, optional (default = False)
        Whether to *also* perform captioning in backward direction. Default is
        ``False`` -- only forward captioning is performed. When ``True``, a
        clone of textual head is created, which does not share weights with
        "forward" model except input and output embeddings.
    """

    def __init__(
        self,
        visual: VisualBackbone,
        textual: TextualHead,
        beam_size: int = 5,
        max_decoding_steps: int = 30,
        sos_index: int = 1,
        eos_index: int = 2,
        caption_backward: bool = False,
    ):
        super().__init__()
        self.visual = visual
        self.textual = textual
        self.padding_idx = self.textual.padding_idx
        self.caption_backward = caption_backward

        self.visual_projection = nn.Linear(
            self.visual.visual_feature_size, self.textual.textual_feature_size
        )
        self.loss = nn.CrossEntropyLoss(ignore_index=self.padding_idx)

        # Clone the textual module for backward direction if doing captioning
        # in both directions (separately).
        if self.caption_backward:
            self.backward_textual = copy.deepcopy(self.textual)
            self.backward_textual.embedding = self.textual.embedding
            self.backward_textual.output = self.textual.output

        # These boundary indices are needed for beam search.
        self.sos_index = sos_index
        self.eos_index = eos_index
        self.beam_search = AutoRegressiveBeamSearch(
            self.eos_index, beam_size=5, max_steps=max_decoding_steps
        )

    def forward(self, batch: ImageCaptionBatch) -> Dict[str, Any]:
        r"""
        Given a batch of images and captions, compute log likelihood loss per
        caption token during training. During inference, given a batch of
        images, decode the most likely caption in forward direction through
        beam search decoding.

        Parameters
        ----------
        batch: virtex.data.structures.ImageCaptionBatch
            A batch of images and (optionally) ground truth caption tokens.

        Returns
        -------
        Dict[str, Any]

            A dict with the following structure, containing loss for optimization,
            loss components to log directly to tensorboard, and optionally
            predictions.

            .. code-block::

                {
                    "loss": torch.Tensor,
                    "loss_components": {
                        "captioning_forward": torch.Tensor,
                        "captioning_backward": torch.Tensor, (optional)
                    },
                    "predictions": torch.Tensor
                }
        """

        # shape: (batch_size, visual_feature_size, ...)
        visual_features = self.visual(batch["image"])
        batch_size = visual_features.size(0)

        # shape: (batch_size, ..., visual_feature_size)
        visual_features = visual_features.view(
            batch["image"].size(0), self.visual.visual_feature_size, -1
        ).permute(0, 2, 1)

        # Now visual and textual features are of same size.
        # shape: (batch_size, ..., textual_feature_size)
        projected_visual_features = self.visual_projection(visual_features)

        caption_tokens = batch["caption_tokens"]
        caption_lengths = batch["caption_lengths"]

        # shape: (batch_size, max_caption_length, vocab_size)
        output_logits = self.textual(
            caption_tokens, caption_lengths, projected_visual_features
        )
        loss = self.loss(
            output_logits[:, :-1].contiguous().view(-1, self.textual.vocab_size),
            caption_tokens[:, 1:].contiguous().view(-1),
        )
        output_dict: Dict[str, Any] = {
            "loss": loss,
            # Single scalar per batch for logging in training script.
            "loss_components": {"captioning_forward": loss.clone().detach()},
        }
        # Do captioning in backward direction if specified.
        if self.caption_backward:
            backward_caption_tokens = batch["noitpac_tokens"]

            backward_output_logits = self.backward_textual(
                backward_caption_tokens,
                caption_lengths,
                projected_visual_features,
            )
            backward_loss = self.loss(
                backward_output_logits[:, :-1]
                .contiguous()
                .view(-1, self.textual.vocab_size),
                backward_caption_tokens[:, 1:].contiguous().view(-1),
            )
            output_dict["loss"] += backward_loss

            # Single scalar per batch for logging in training script.
            output_dict["loss_components"].update(
                captioning_backward=backward_loss.clone().detach()
            )

            # During evaluation, get beam search predictions for forward model.
            # Predictions from forward transformer will be shifted right by one
            # time-step.
            if not self.training:
                start_predictions = projected_visual_features.new_full(
                    (batch_size,), self.sos_index
                ).long()
                # Add image features as a default argument to match callable
                # signature accepted by beam search class (partial captions only).
                beam_search_step = functools.partial(
                    self.beam_search_step, projected_visual_features
                )
                all_top_k_predictions, _ = self.beam_search.search(
                    start_predictions, beam_search_step
                )
                best_beam = all_top_k_predictions[:, 0, :]
                output_dict["predictions"] = best_beam

        return output_dict

    def beam_search_step(
        self, projected_visual_features: torch.Tensor, partial_captions: torch.Tensor
    ) -> torch.Tensor:
        r"""
        Given visual features and a batch of (assumed) partial captions, predict
        the distribution over vocabulary tokens for next time-step. This method
        is used by :class:`~virtex.utils.beam_search.AutoRegressiveBeamSearch`.

        Parameters
        ----------
        projected_visual_features: torch.Tensor
            A tensor of shape ``(batch_size, ..., textual_feature_size)``
            with visual features already projected to ``textual_feature_size``.
        partial_captions: torch.Tensor
            A tensor of shape ``(batch_size * beam_size, timesteps)``
            containing tokens predicted so far -- one for each beam. We need all
            prior predictions because our model is auto-regressive.

        Returns
        -------
        torch.Tensor
            A tensor of shape ``(batch_size * beam_size, vocab_size)`` -- output
            distribution over tokens for next time-step.
        """

        batch_size, num_features, textual_feature_size = (
            projected_visual_features.size()
        )
        # Expand and repeat image features while doing beam search.
        beam_size = int(partial_captions.size(0) / batch_size)
        if beam_size > 1:
            projected_visual_features = projected_visual_features.unsqueeze(1).repeat(
                1, beam_size, 1, 1
            )
            projected_visual_features = projected_visual_features.view(
                batch_size * beam_size, num_features, textual_feature_size
            )

        # Provide caption lengths as current length (irrespective of predicted
        # EOS/padding tokens). shape: (batch_size, )
        caption_lengths = torch.ones_like(partial_captions)
        if len(caption_lengths.size()) == 2:
            caption_lengths = caption_lengths.sum(1)
        else:
            # Add a time-step. shape: (batch_size, 1)
            partial_captions = partial_captions.unsqueeze(1)

        # shape: (batch_size * beam_size, partial_caption_length, vocab_size)
        output_logits = self.textual(
            partial_captions, caption_lengths, projected_visual_features
        )
        # Keep features for last time-step only, we only care about those.
        output_logits = output_logits[:, -1, :]

        # Return logprobs as required by `AutoRegressiveBeamSearch`.
        # shape: (batch_size * beam_size, vocab_size)
        next_logprobs = F.log_softmax(output_logits, dim=1)

        # Set logprobs of last predicted tokens as high negative value to avoid
        # repetition in caption.
        for index in range(batch_size * beam_size):
            next_logprobs[index, partial_captions[index, -1]] = -1000000

        return next_logprobs

    def log_predictions(
        self, batch: ImageCaptionBatch, tokenizer: SentencePieceBPETokenizer
    ) -> str:

        self.eval()
        with torch.no_grad():
            predictions = self.forward(batch)["predictions"]
        self.train()

        predictions_str = ""
        for tokens, preds in zip(batch["caption_tokens"], predictions):
            predictions_str += f"""
                Caption tokens : {tokenizer.decode(tokens.tolist())}
                Predictions (f): {tokenizer.decode(preds.tolist())}

                """
        return predictions_str


class ForwardCaptioningModel(CaptioningModel):
    r"""
    Convenient extension of :class:`~virtex.models.captioning.CaptioningModel`
    for better readability: this passes ``caption_backward=False`` to super class.
    """

    def __init__(
        self,
        visual: VisualBackbone,
        textual: TextualHead,
        beam_size: int = 5,
        max_decoding_steps: int = 30,
        sos_index: int = 1,
        eos_index: int = 2,
    ):
        super().__init__(
            visual,
            textual,
            beam_size=beam_size,
            max_decoding_steps=max_decoding_steps,
            sos_index=sos_index,
            eos_index=eos_index,
            caption_backward=False,
        )


class BidirectionalCaptioningModel(CaptioningModel):
    r"""
    Convenient extension of :class:`~virtex.models.captioning.CaptioningModel`
    for better readability: this passes ``caption_backward=True`` to super class.
    """

    def __init__(
        self,
        visual: VisualBackbone,
        textual: TextualHead,
        beam_size: int = 5,
        max_decoding_steps: int = 30,
        sos_index: int = 1,
        eos_index: int = 2,
    ):
        super().__init__(
            visual,
            textual,
            beam_size=beam_size,
            max_decoding_steps=max_decoding_steps,
            sos_index=sos_index,
            eos_index=eos_index,
            caption_backward=True,
        )
