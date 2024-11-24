# General
import torch
import torch.nn.functional as F
import os
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
import copy

# Local Modules
from .utils import *
# LoRA moduels
from .lora import *
from .lora.loralib.utils import apply_lora, get_lora_parameters, lora_state_dict, save_lora, load_lora
from .lora.loralib import layers as lora_layers
# BitFit
from .bitfit.bitfit import apply_BitFit
# Meta-Adapter
from .meta_adapter.meta_adapter import MetaAdapter
# CLIP
from .clip import *
from .model import get_text_target_features, get_vision_target_features

def eval_model(args, model, logit_scale, loader, target_features, support_img_loader=None, meta_query=None, meta_key=None):
    """
    Zero-shot evaluation of CLIP model
    
    Returns:
    - accuracy (float): Overall accuracy
    - images (np.array): Images from the test set
    - targets (np.array): True labels
    - predictions (np.array): Predicted labels
    - similarities (np.array): Similarity scores
    """
    if (meta_key is None or meta_query is None) and support_img_loader is None:
        raise ValueError("Neither support_img_loader nor (meta_key and meta_query) provided. Please provide one of them.")

    acc = 0.
    tot_samples = 0
    all_images = []
    all_targets = []
    all_predictions = []
    all_similarities = []
    
    # Evaluation mode
    model.eval()
    with torch.no_grad():
        # Extract Query - Key pairs for Meta-Adapter (if enabled and key/query not provided)
        if args.enable_MetaAdapter and (meta_query is None or meta_key is None) :
            support_features = get_vision_target_features(model, support_img_loader) # Support features
            meta_query = target_features # Category embeddings
            meta_key = support_features.reshape(meta_query.shape[0], -1, meta_query.shape[1]) # Support embedding

        # Evaluation loop
        for i, (images, target, target_f) in enumerate(loader):
            images, target = images.cuda(), target.cuda()
            with torch.amp.autocast(device_type="cuda", dtype=torch.float16):
                image_features = model.encode_image(images)
                image_features = image_features/image_features.norm(dim=-1, keepdim=True)
            
            # calculate cosine similarity and predictions
            if args.enable_MetaAdapter:
                with torch.amp.autocast(device_type="cuda", dtype=torch.float16):
                    meta_adaptation = model.meta_adapter(meta_query, meta_key, meta_key)
                cosine_similarity = logit_scale * image_features @ meta_adaptation.t()
            else :
                # directly get similarity scores with class features
                cosine_similarity = logit_scale * image_features @ target_features.t()

            # get predictions
            pred = cosine_similarity.argmax(dim=-1)
            # update accuracy
            acc += cls_acc(cosine_similarity, target) * len(cosine_similarity)
            tot_samples += len(cosine_similarity)
            
            # collect data 
            all_images.extend(images.cpu().numpy())
            all_targets.extend(target.cpu().numpy())
            all_predictions.extend(pred.cpu().numpy())
            all_similarities.extend(cosine_similarity.cpu().numpy())
     
    # calculate final accuracy       
    acc /= tot_samples
    
    # convert data to numpy arrays
    images = np.array(all_images)
    targets = np.array(all_targets)
    predictions = np.array(all_predictions)
    similarities = np.array(all_similarities)

    return acc, images, targets, predictions, similarities


def train_model(args, model, logit_scale, dataset, train_loader, val_loader, test_loader, target_loader, target_features, task_type):
    """
    Run CLIP with chosen modules (LoRA, Meta-Adapter, etc.)
    (currently is a Copy from run_lora from LoRa-challengingDatasets/modules/lora/lora.py)
    """
    # attention_mask = model.clip_model.transformer.resblocks[0].attention_mask.cpu().detach().numpy()
    
    # Extract Query - Key pairs for Meta-Adapter
    if args.enable_MetaAdapter:
        model.eval()
        with torch.no_grad():
            support_features = get_vision_target_features(model, val_loader)
            meta_query = target_features # Category embeddings
            meta_key = support_features.reshape(meta_query.shape[0], -1, meta_query.shape[1]) # Support embedding

    # Set up optimizer and scheduler
    total_iters = args.n_iters * args.shots
    optimizer = torch.optim.AdamW(model.parameters(), weight_decay=1e-2, betas=(0.9, 0.999), lr=args.lr)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, total_iters, eta_min=1e-6)

    # training model
    scaler = torch.cuda.amp.GradScaler()
    count_iters = 0
    best_acc = 0
    best_model = model

    while count_iters < total_iters:
        model.train()
        acc_train = 0
        tot_samples = 0
        loss_epoch = 0.
        for i, (images, target, target_f) in enumerate(tqdm(train_loader, desc=f'Training')):
            # Load data on GPU
            images, target = images.cuda(), target.cuda()
            # Load Label features
            if  (args.enable_lora and task_type == 'image2text' and (args.encoder == 'text' or args.encoder == 'both')) or \
                (args.enable_lora and task_type == 'image2image' and (args.encoder == 'vision' or args.encoder == 'both')) or \
                (args.enable_BitFit) :
                target_features = get_text_target_features(model, dataset) if task_type == 'image2text' else get_vision_target_features(model, target_loader)
            # Forward the batch
            if args.encoder == 'vision' or args.encoder == 'both':
                with torch.amp.autocast(device_type="cuda", dtype=torch.float16):
                    image_encoding = model.encode_image(images)
                    image_features = image_encoding/image_encoding.norm(dim=-1, keepdim=True)

            if args.enable_MetaAdapter:
                # Forward through Meta-Adapter
                with torch.amp.autocast(device_type="cuda", dtype=torch.float16):
                    meta_adaptation = model.meta_adapter(meta_query, meta_key, meta_key)
                cosine_similarity = logit_scale * image_features @ meta_adaptation.T
            else :
                # directly get similarity scores with class features
                cosine_similarity = logit_scale * image_features @ target_features.T
            
            loss = F.cross_entropy(cosine_similarity, target)
            acc_train += cls_acc(cosine_similarity, target) * target.shape[0]
            loss_epoch += loss.item() * target.shape[0]
            tot_samples += target.shape[0]
            
            optimizer.zero_grad()
            scaler.scale(loss).backward(retain_graph=True)
            scaler.step(optimizer)

            scaler.update()
            scheduler.step()
            
            # update cache_keys
            if args.enable_MetaAdapter :
                with torch.no_grad():
                    for tar, feat in zip(target, image_features):
                        meta_key[tar] = torch.cat([feat[None, :], meta_key[tar][:meta_key.shape[1] - 1]], dim=0)

            count_iters += 1
            if count_iters == total_iters:
                break

        if count_iters < total_iters:
            acc_train /= tot_samples
            loss_epoch /= tot_samples
            current_lr = scheduler.get_last_lr()[0]
            print(f"LR: {current_lr:.6f}, Acc: {acc_train:.4f}, Loss: {loss_epoch:.4f}")

        model.eval()
        acc_val, _, _, _, _ = eval_model(args, model, logit_scale, val_loader, val_loader, target_features, meta_query=meta_query, meta_key=meta_key)
        print(f"**** Validation accuracy: {acc_val:.2f}. ****\n")
        
        # Save best model (maximizing validation accuracy) ((!!! THAT'S PRETTY WRONG IN FEW-SHOT CONTEXT !!!))
        if acc_val > best_acc:
            best_acc = acc_val
            best_model = copy.deepcopy(model).to('cpu')

    best_model = best_model.cuda()
    acc_test, images, targets, predictions, similarities = eval_model(args, best_model, logit_scale, test_loader, val_loader, target_features, meta_query=meta_query, meta_key=meta_key)  
    print(f"**** Test accuracy: {acc_test:.2f}. ****\n") 
    # plot_confusion_matrix(targets, predictions, dataset.classnames)
    # plot_topk_images_for_class(images, targets, predictions, similarities, dataset.classnames, 3, "correct")
    # plot_topk_images_for_class(images, targets, predictions, similarities, dataset.classnames, 3, "incorrect")
    # plot_topk_images(images, targets, predictions, similarities, dataset.classnames, 5, "correct")
    # plot_topk_images(images, targets, predictions, similarities, dataset.classnames, 5, "incorrect")
    
    if args.save_path != None:
        full_path = os.path.join(args.save_path, str(args.filename) + '.pth')
        torch.save({'model_state_dict':best_model.state_dict()}, full_path)
        print("Model saved => ", full_path)
        
    return



