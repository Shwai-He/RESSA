import argparse
import random
import time
import os

import numpy as np
import torch
import torch.backends.cudnn as cudnn

import lavis.tasks as tasks
from lavis.common.config import Config
from lavis.common.dist_utils import get_rank, init_distributed_mode
from lavis.common.logger import setup_logger
from lavis.common.optims import (
    LinearWarmupCosineLRScheduler,
    LinearWarmupStepLRScheduler,
)
from lavis.common.utils import now

from lavis.datasets.builders import *
from lavis.models import *
from lavis.processors import *
from lavis.runners.runner_base import RunnerBase
from lavis.tasks import *



def parse_args():
    parser = argparse.ArgumentParser(description="Training")

    parser.add_argument("--cfg-path", required=True, help="path to configuration file.")
    parser.add_argument(
        "--options",
        nargs="+",
        help="override some settings in the used config, the key-value pair "
        "in xxx=yyy format will be merged into config file (deprecate), "
        "change to --cfg-options instead.",
    )

    parser.add_argument(
        "--side_pretrained_weight",
        type=str,
        default=None,
        help="The pre-trained config for the distilled transformer."
    )

    parser.add_argument(
        "--vit_side_pretrained_weight",
        type=str,
        default=None,
        help="The pre-trained config for the distilled transformer."
    )

    parser.add_argument(
        "--distillation_init",
        type=str,
        default="sum",
        help="Whether to init the distilled transformer."
    )

    parser.add_argument(
        "--distilled_block_ids",
        type=str,
        default=None,
        help="The layer assignment to merge the distilled transformer."
    )

    parser.add_argument(
        "--distilled_block_weights",
        type=str,
        default=None,
        help="The weight assignments to merge the distilled transformer."
    )

    parser.add_argument(
        "--modules_to_merge",
        type=str,
        default=".*",
        help="The type of modules to merge."
    )

    parser.add_argument(
        "--permute_before_merge",
        action="store_true",
        default=False,
        help="Whether to permute the layers before merging (permute based on the first layer)"
    )

    parser.add_argument(
        "--permute_on_block_before_merge",
        action="store_true",
        default=False,
        help="Whether to permute the layers before merging (permute independently based on blocks)"
    )

    parser.add_argument(
        "--job_id",
        type=str,
        default=None,
        help="The id of the Job"
    )

    parser.add_argument(
        "--vit_ffn_ratio", type=float, default=1.0
    )

    parser.add_argument(
        "--distilled_merge_ratio", type=float, default=0.5
    )

    parser.add_argument(
        "--exact", action="store_true"
    )

    parser.add_argument(
        "--normalization", action="store_true"
    )

    parser.add_argument(
        "--metric", type=str, default="dot"
    )

    parser.add_argument(
        "--distill_merge_ratio", type=float, default=0.5
    )

    parser.add_argument(
        "--to_one", action="store_true"
    )

    parser.add_argument(
        "--importance", action="store_true"
    )

    parser.add_argument(
        "--num_data", type=int, default=64
    )

    parser.add_argument(
        "--power", type=int, default=2
    )

    parser.add_argument(
        "--num_logits", type=int, default=1
    )

    parser.add_argument(
        "--get_derivative_info", action="store_true"
    )

    parser.add_argument(
        "--get_activation_info", action="store_true"
    )

    parser.add_argument(
        "--use_input_activation", action="store_true"
    )

    parser.add_argument(
        "--save_pruned_indices", action="store_true"
    )

    parser.add_argument(
        "--vit_pruned_indices", type=str, default=None
    )

    parser.add_argument(
        "--t5_pruned_indices", type=str, default=None
    )

    parser.add_argument(
        "--save_importance_measure", action="store_true"
    )

    parser.add_argument(
        "--vit_importance_measure", type=str, default=None
    )

    parser.add_argument(
        "--t5_importance_measure", type=str, default=None
    )

    parser.add_argument(
        "--vision_weight", type=float, default=0.0
    )

    parser.add_argument(
        "--save_final_activations", action="store_true"
    )

    args = parser.parse_args()
    # if 'LOCAL_RANK' not in os.environ:
    #     os.environ['LOCAL_RANK'] = str(args.local_rank)

    return args


def setup_seeds(config):
    seed = config.run_cfg.seed + get_rank()

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

    cudnn.benchmark = False
    cudnn.deterministic = True


def get_final_activations(args, cfg, task, model, datasets):
    runner = RunnerBase(
        cfg=cfg, job_id=None, task=task, model=model, datasets=datasets
    )
    start = time.time()

    print("Start to get final activation")
    outputs = runner.get_last_activations(num_data=args.num_data, power=args.power)

    end = time.time()
    print(f"Finish get final activation, using {end - start:.3f}s")

    return outputs


def main():
    # allow auto-dl completes on main process without timeout when using NCCL backend.
    # os.environ["NCCL_BLOCKING_WAIT"] = "1"

    args = parse_args()

    # set before init_distributed_mode() to ensure the same job_id shared across all ranks.
    if args.job_id is not None:
        job_id = args.job_id
    else:
        job_id = now()

    cfg = Config(args)

    init_distributed_mode(cfg.run_cfg)

    setup_seeds(cfg)

    # set after init_distributed_mode() to only log on master.
    setup_logger()

    cfg.pretty_print()

    task = tasks.setup_task(cfg)
    datasets = task.build_datasets(cfg)
    model = task.build_model(cfg)


    woodfisher_pruner = None

    is_strct_pruning = False
    if args.distillation_init is not None:
        is_strct_pruning = "unstrct" in args.distillation_init
    
    if "woodfisher" in args.distillation_init and args.get_derivative_info:
        print("Setup for computing wood fisher")

        runner = RunnerBase(
            cfg=cfg, job_id=None, task=task, model=model, datasets=datasets
        )

        start = time.time()

        print("Start to compute wood fisher")
        wood_dataloader = runner.get_dataloader_for_importance_computation(num_data=args.num_data, power=args.power)

        
        woodfisher_pruner = WoodFisher(
            runner.unwrap_dist_model(runner.model).eval(), 
            wood_dataloader, 
            num_samples=args.num_data, fisher_damp=1e-3, fisher_parts=5, fisher_optimized=False, ignore_keys=[]
        )

        importance_scores = woodfisher_pruner.compute_fisher_inv_and_importance_score()

        vit_derivative_info = {k[15:]: v for k, v in derivative_info.items() if k.startswith("visual_encoder")} # filter out some info that is not for this transformer
        t5_derivative_info = {k[9:]: v for k, v in derivative_info.items() if k.startswith("t5_model")} # filter out some info that is not for this transformer

        end = time.time()
        print(f"Finish computing wood fisher, using {end - start:.3f}s")

    elif args.get_derivative_info:
        print("Setup for computing derivatice info")

        runner = RunnerBase(
            cfg=cfg, job_id=None, task=task, model=model, datasets=datasets
        )

        start = time.time()

        print("Start to compute derivatice info")
        derivative_info = runner.get_data_derivative(num_data=args.num_data, power=args.power, num_logits=args.num_logits, vision_weight=args.vision_weight)

        vit_derivative_info = {k[15:]: v for k, v in derivative_info.items() if k.startswith("visual_encoder")} # filter out some info that is not for this transformer
        t5_derivative_info = {k[9:]: v for k, v in derivative_info.items() if k.startswith("t5_model")} # filter out some info that is not for this transformer

        end = time.time()
        print(f"Finish computing derivatice info, using {end - start:.3f}s")
        # for n, p in derivative_info.items():
        #     print(n, p.shape)

    elif args.get_activation_info:
        print("Setup for computing activation info")

        runner = RunnerBase(
            cfg=cfg, job_id=None, task=task, model=model, datasets=datasets
        )

        start = time.time()

        print("Start to compute activation info")
        input_representations, output_representations = runner.get_activations(num_data=args.num_data, power=args.power)

        derivative_info = runner.convert_activation_to_importance(input_representations, output_representations, args.use_input_activation)

        vit_derivative_info = {k[15:]: v for k, v in derivative_info.items() if k.startswith("visual_encoder")} # filter out some info that is not for this transformer
        t5_derivative_info = {k[9:]: v for k, v in derivative_info.items() if k.startswith("t5_model")} # filter out some info that is not for this transformer

        end = time.time()
        print(f"Finish computing activation info, using {end - start:.3f}s")
    else:
        derivative_info = None
        vit_derivative_info = None
        t5_derivative_info = None

    orig_total_size = sum(
        param.numel() for param in model.parameters()
    )

    vit_importance_measure = None
    if args.vit_importance_measure is not None:
        vit_importance_measure = torch.load(args.vit_importance_measure)
        vit_importance_measure = vit_importance_measure["vit"]

    vit_pruned_indices = None
    if args.vit_pruned_indices is not None:
        vit_pruned_indices = torch.load(args.vit_pruned_indices)
        vit_pruned_indices = vit_pruned_indices["vit"]
    
    model.visual_encoder, vit_prune_indices, vit_importance_measure = vit_modify_with_weight_init(model.visual_encoder, args, model.freeze_vit, model.vit_precision, vit_derivative_info, pruned_indices=vit_pruned_indices, importance_measure=vit_importance_measure, woodfisher_pruner=woodfisher_pruner)
    
    t5_importance_measure = None
    if args.t5_importance_measure is not None:
        t5_importance_measure = torch.load(args.t5_importance_measure)
        t5_importance_measure = t5_importance_measure["t5"]

    t5_pruned_indices = None
    if args.t5_pruned_indices is not None:
        t5_pruned_indices = torch.load(args.t5_pruned_indices)
        t5_pruned_indices = t5_pruned_indices["t5"]

    model.t5_model, t5_prune_indices, t5_importance_measure = t5_modify_with_weight_init(model.t5_model, args, t5_derivative_info, to_bf16=True, pruned_indices=t5_pruned_indices, importance_measure=t5_importance_measure, woodfisher_pruner=woodfisher_pruner)

    for name, param in model.t5_model.named_parameters():
        param.requires_grad = False

    if args.save_final_activations:
        outputs = get_final_activations(args=args, cfg=cfg, task=task, model=model, datasets=datasets)

        saved_folder = "final_activations"
        os.makedirs(saved_folder, exist_ok=True)

        torch.save(outputs, os.path.join(saved_folder, job_id + ".pth"))

        print(os.path.join(saved_folder, job_id + ".pth"))

        exit()

    if args.save_pruned_indices:

        saved_folder = "pruned_indices"
        os.makedirs(saved_folder, exist_ok=True)

        pruned_indices = {
            "t5": t5_prune_indices,
            "vit": vit_prune_indices,
        }

        torch.save(pruned_indices, os.path.join(saved_folder, job_id + ".pth"))

        print(os.path.join(saved_folder, job_id + ".pth"))

        exit()

    if args.save_importance_measure:
        saved_folder = "importance_measure"
        os.makedirs(saved_folder, exist_ok=True)

        importance_measure = {
            "vit": vit_importance_measure,
            "t5": t5_importance_measure,
        }

        torch.save(importance_measure, os.path.join(saved_folder, job_id + ".pth"))

        print(os.path.join(saved_folder, job_id + ".pth"))

        exit()

    if is_strct_pruning:
        distilled_total_size = sum(
            (param != 0).float().sum() for param in model.parameters()
        )
    else:
        # only prune qformer for structural pruning
        model.Qformer, model.t5_proj = qformer_pruning(
            model.Qformer, 
            model.t5_proj, 
            model.init_Qformer, 
            vit_prune_indices["P_vit_res"] if vit_prune_indices is not None else None, 
            t5_prune_indices["P_res"] if t5_prune_indices is not None else None
        )

        distilled_total_size = sum(
            param.numel() for param in model.parameters()
        )

    runner = RunnerBase(
        cfg=cfg, job_id=job_id, task=task, model=model, datasets=datasets
    )

    runner.orig_total_size = orig_total_size
    runner.distilled_total_size = distilled_total_size

    runner.evaluate(skip_reload=True)


if __name__ == "__main__":
    # time.sleep(10000)
    main()
