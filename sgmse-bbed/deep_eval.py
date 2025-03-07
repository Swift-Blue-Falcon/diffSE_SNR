import numpy as np
import glob
from soundfile import read, write
from tqdm import tqdm
from pesq import pesq
from torchaudio import load
import torch
from argparse import ArgumentParser
from os.path import join
import pandas as pd

from sgmse.data_module import SpecsDataModule
from sgmse.sdes import OUVESDE
from sgmse.model import ScoreModel
from sgmse.snr_estimator import SNRModel

from pesq import pesq
from pystoi import stoi

from utils import energy_ratios, ensure_dir, print_mean_std

if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument("--destination_folder", type=str, help="Name of destination folder.")
    parser.add_argument("--test_dir", type=str, required=True, help='Directory containing the test data')
    parser.add_argument("--ckpt", type=str, help='Path to model checkpoint.')
    parser.add_argument("--sampler_type", type=str, choices=("pc", "ode"), default="pc",
                        help="Specify the sampler type")
    parser.add_argument("--predictor", type=str,
                        default="reverse_diffusion", help="Predictor class for the PC sampler.")
    parser.add_argument("--reverse_starting_point", type=float, default=1.0, help="Starting point for the reverse SDE.")
    parser.add_argument("--force_N", type=int, default=0, help="Force the number of reverse steps for modified reverse starting point.")
    parser.add_argument("--corrector", type=str, choices=("ald", "none"), default="ald",
                        help="Corrector class for the PC sampler.")
    parser.add_argument("--corrector_steps", type=int, default=1, help="Number of corrector steps")
    parser.add_argument("--snr", type=float, default=0.5, help="SNR value for annealed Langevin dynmaics.")
    parser.add_argument("--N", type=int, default=30, help="Number of reverse steps")
    parser.add_argument("--atol", type=float, default=1e-5, help="Absolute tolerance for the ODE sampler")
    parser.add_argument("--rtol", type=float, default=1e-5, help="Relative tolerance for the ODE sampler")
    parser.add_argument("--timestep_type", type=str, default='linear', help="timestep for sampling")
    parser.add_argument("--correct_stepsize", dest='correct_stepsize', action='store_true',
                        help="Use correct stepsize timestep[i] - timestep[i+1]")
    parser.add_argument("--no_correct_stepsize", dest='correct_stepsize', action="store_false",
                        help="Use correct stepsize timestep[i] - timestep[i+1]")
    parser.add_argument("--modeltype", type=str, choices=["bbed", "sebridge", 'sebridge_v2', 'sebridge_v3'], default="bbed")
    parser.add_argument("--oracle", type=bool, default=False, help='Use SNR oracle')
    parser.set_defaults(correct_stepsize=True)


    args = parser.parse_args()

    clean_dir = join(args.test_dir, "clean")
    noisy_dir = join(args.test_dir, "noisy")

    checkpoint_file = args.ckpt
    
    #please change this directory
    target_dir = args.destination_folder

    # ensure_dir(target_dir + "files/")

    # Settings
    sr = 16000
    sampler_type = args.sampler_type
    N = args.N
    correct_stepsize = args.correct_stepsize
    predictor = args.predictor
    timestep_type = args.timestep_type
    corrector = args.corrector
    corrector_steps = args.corrector_steps
    snr = args.snr
    reverse_starting_point = args.reverse_starting_point
    atol = args.atol
    rtol = args.rtol

    # Load score model
    model = ScoreModel.load_from_checkpoint(
        checkpoint_file, base_dir="",
        batch_size=16, num_workers=0, kwargs=dict(gpu=False)
    )
    model.eval(no_ema=False)
    model.cpu()

    noisy_files = sorted(glob.glob('{}/*.wav'.format(noisy_dir)))



    if model.sde.__class__.__name__ == 'OUVESDE':
        model.sde._T = reverse_starting_point
    else:  
        model.sde.T = reverse_starting_point
    delta_t = 1/N
    N = int(reverse_starting_point / delta_t)

    if args.force_N:
        N = args.force_N


    _pesq = [0] * 9

    data = {"filename": [], "pesq_-5": [], "pesq_0": [], "pesq_5": [], "pesq_10": [], "pesq_15": [], "pesq_20": [], "pesq_25": [], "pesq_30": [], "pesq_35": []}
    
    for cnt, noisy_file in tqdm(enumerate(noisy_files)):
        filename = noisy_file.split('/')[-1]
        data["filename"].append(filename)
        
        # Load wav
        x_def, _ = load(join(clean_dir, filename))
        y_def, _ = load(noisy_file)
        y0_def = y_def - x_def

        for SNR in range(0, 41, 5):
            
            x = x_def
            y = x_def + y0_def * 10 ** (-SNR / 20)

            clean_rms = 1
            noise_rms = 10 ** ((-SNR + 5) / 20)

            x_hat = model.enhance(x, y, sampler_type=sampler_type, predictor=predictor, 
                    corrector=corrector, corrector_steps=corrector_steps, N=N, snr=snr,
                    atol=atol, rtol=rtol, timestep_type=timestep_type, correct_stepsize=correct_stepsize, oracle=args.oracle, clean_rms=clean_rms, noise_rms=noise_rms)


            # Convert to numpy
            x = x.squeeze().cpu().numpy()
            y = y.squeeze().cpu().numpy()
            n = y - x

            # Write enhanced wav file
            write(join(target_dir, "{0:02d}".format(SNR-5), filename), x_hat, 16000)

            # Append metrics to data frame
            try:
                p = pesq(sr, x, x_hat, 'wb')
            except: 
                p = float("nan")

            _pesq[SNR//5] += p
            
            print("{0} | {1:.3f}".format(SNR-5, _pesq[SNR//5]/(cnt + 1)))


            data[f"pesq_{SNR-5}"].append(p)
            # data["estoi"].append(stoi(x, x_hat, sr, extended=True))
            # data["si_sdr"].append(energy_ratios(x_hat, x, n)[0])
            # data["si_sir"].append(energy_ratios(x_hat, x, n)[1])
            # data["si_sar"].append(energy_ratios(x_hat, x, n)[2])

    # Save results as DataFrame
    df = pd.DataFrame(data)
    df.to_csv(join(target_dir, "_results_deep.csv"), index=False)

    # # Save average results
    text_file = join(target_dir, "_avg_results_deep.txt")
    with open(text_file, 'w') as file:

        for SNR in range(0, 41, 5):
            file.write("PESQ_{0}: {1} \n".format(SNR-5, print_mean_std(data[f"pesq_{SNR-5}"], decimal=3)))
            # file.write("ESTOI: {} \n".format(print_mean_std(data["estoi"])))
            # file.write("SI-SDR: {} \n".format(print_mean_std(data["si_sdr"])))
            # file.write("SI-SIR: {} \n".format(print_mean_std(data["si_sir"])))
            # file.write("SI-SAR: {} \n".format(print_mean_std(data["si_sar"])))

    # # Save settings
    # text_file = join(target_dir, "_settings.txt")
    # with open(text_file, 'w') as file:
    #     file.write("checkpoint file: {}\n".format(checkpoint_file))
    #     file.write("sampler_type: {}\n".format(sampler_type))
    #     file.write("predictor: {}\n".format(predictor))
    #     file.write("corrector: {}\n".format(corrector))
    #     file.write("corrector_steps: {}\n".format(corrector_steps))
    #     file.write("N: {}\n".format(N))
    #     file.write("N forced: {}\n".format(args.force_N))
    #     file.write("Reverse starting point: {}\n".format(reverse_starting_point))
    #     file.write("snr: {}\n".format(snr))
    #     file.write("timestep type: {}\n".format(timestep_type))
    #     file.write("correct_stepsize: {}\n".format(correct_stepsize))
    #     if sampler_type == "ode":
    #         file.write("atol: {}\n".format(atol))
    #         file.write("rtol: {}\n".format(rtol))
