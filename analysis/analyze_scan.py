"""Validate recorded measurements, draw plots and build the PDF report.
Run after scan_benchmark.exe; accepts an optional results directory.
Plots need numpy and matplotlib. The Windows PDF also needs reportlab and pypdf.
"""
import csv
import argparse
import json
from pathlib import Path
from collections import defaultdict
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
ROOT=Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('results_directory', nargs='?', type=Path, default=ROOT.parent/'results'/'stride-scan')
parser.add_argument('--plots-only', action='store_true', help='Skip the Windows PDF report')
args = parser.parse_args()
OUT = args.results_directory.resolve()
FIG=OUT/'figures'; FIG.mkdir(exist_ok=True)
meta=json.loads((OUT/'benchmark_metadata.json').read_text())
if meta.get('platform') == 'Linux' and not args.plots_only:
    parser.error('Use --plots-only for Ubuntu results.')
line_bytes = meta['line_bytes_cpuid']
rows=list(csv.DictReader((OUT/'raw_timings.csv').open()))
strides=[1,2,4,8,16,32,64,128,256,512,1024]
groups=defaultdict(list)
checks=defaultdict(set)
seen=set()
for r in rows:
    key=(r['condition'],int(r['stride_bytes']))
    assert key[0] in ('flushed','repeat_scan') and key[1] in strides
    assert int(r['reads'])==meta['reads_per_scan']==200000
    assert int(r['address_span_bytes'])==(200000-1)*key[1]+1<=meta['buffer_bytes']
    assert int(r['selected_cache_lines'])==min(200000,(int(r['address_span_bytes'])+line_bytes-1)//line_bytes)
    assert float(r['elapsed_ns'])>0
    identity=(int(r['round']),*key)
    assert identity not in seen
    seen.add(identity)
    groups[key].append(float(r['elapsed_ns'])/1e6)
    checks[key[1]].add(int(r['checksum']))
assert len(rows)==2*len(strides)*meta['repetitions']
assert all(len(v)==1 for v in checks.values())
assert all(len(groups[c,s])==meta['repetitions'] for c in ('flushed','repeat_scan') for s in strides)
stats={}
for (condition,stride),values in sorted(groups.items()):
    v=np.array(values)
    stats[condition,stride]=dict(condition=condition,stride_bytes=stride,n=len(v),
        mean_ms=float(v.mean()),median_ms=float(np.median(v)),sample_sd_ms=float(v.std(ddof=1)),
        min_ms=float(v.min()),p05_ms=float(np.percentile(v,5)),p95_ms=float(np.percentile(v,95)),
        max_ms=float(v.max()),cv_percent=float(100*v.std(ddof=1)/v.mean()),
        median_ns_per_read=float(np.median(v)*1e6/200000))
with (OUT/'summary.csv').open('w',newline='') as f:
    writer=csv.DictWriter(f,fieldnames=list(next(iter(stats.values()))))
    writer.writeheader();writer.writerows(stats.values())
overhead=[float(r['elapsed_ns']) for r in csv.DictReader((OUT/'timer_overhead.csv').open())]
validation=dict(recorded_scans=len(rows),groups=len(groups),samples_per_group=meta['repetitions'],
    checksums_consistent=True,bounds_valid=True,duplicate_measurements=False,
    timer_pair_median_ns=float(np.median(overhead)),timer_pair_p95_ns=float(np.percentile(overhead,95)))
(OUT/'validation.json').write_text(json.dumps(validation,indent=2))

plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.spines.top':False,
                     'axes.spines.right':False,'axes.titleweight':'bold','figure.facecolor':'white'})
BLUE='#185b91'; ORANGE='#cc6b24'
def save(fig,name):
    fig.tight_layout(); fig.savefig(FIG/name,dpi=190,bbox_inches='tight');plt.close(fig)
main=strides[:9]
fig,ax=plt.subplots(figsize=(9,4.1))
med=np.array([stats['flushed',s]['median_ms'] for s in main])
lo=np.array([stats['flushed',s]['p05_ms'] for s in main]);hi=np.array([stats['flushed',s]['p95_ms'] for s in main])
ax.fill_between(main,lo,hi,color=BLUE,alpha=.15,label='5th to 95th percentile of runs')
ax.plot(main,med,'o-',color=BLUE,label='Median')
ax.plot(main,[stats['flushed',s]['mean_ms'] for s in main],'s--',color=ORANGE,label='Mean')
ax.set(xlabel='Stride (bytes)',ylabel='Elapsed time (ms)',title='Initial scan on this laptop | flushed target data',xlim=(0,262))
ax.grid(alpha=.2);ax.legend(loc='upper left',fontsize=9)
save(fig,'01_primary_stride_scan.png')

fig,ax=plt.subplots(figsize=(9,3.8))
for cond,color,label in [('flushed',BLUE,'Flushed'),('repeat_scan',ORANGE,'Immediate repeat scan')]:
    ax.plot(strides,[stats[cond,s]['median_ms'] for s in strides],'o-',color=color,label=label)
ax.set_xscale('log',base=2);ax.set_xticks(strides,labels=[str(s) for s in strides])
ax.set(xlabel='Stride (bytes, logarithmic spacing)',ylabel='Median elapsed time (ms)',
       title=f'Cache preparation comparison | {meta["repetitions"]} runs per point')
ax.grid(alpha=.2);ax.legend(fontsize=9)
save(fig,'02_cache_preparation.png')

fig,ax=plt.subplots(figsize=(9,3.8))
ax.boxplot([groups['flushed',s] for s in strides],tick_labels=[str(s) for s in strides],
           patch_artist=True,boxprops={'facecolor':'#dce9f2'},medianprops={'color':BLUE})
ax.set_yscale('log')
ax.set(xlabel='Stride (bytes)',ylabel='Elapsed time (ms, logarithmic scale)',
       title='Run variability | flushed scans with all outliers retained')
ax.grid(axis='y',alpha=.2)
save(fig,'03_run_variability.png')

if args.plots_only:
    print(f'Plots saved in {FIG}')
    raise SystemExit(0)

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

for name,filename in [('TimesNewRoman','times.ttf'),('TimesNewRoman-Bold','timesbd.ttf'),
                      ('TimesNewRoman-Italic','timesi.ttf'),('TimesNewRoman-BoldItalic','timesbi.ttf')]:
    pdfmetrics.registerFont(TTFont(name,str(Path('C:/Windows/Fonts')/filename)))
pdfmetrics.registerFontFamily('TimesNewRoman',normal='TimesNewRoman',bold='TimesNewRoman-Bold',
                              italic='TimesNewRoman-Italic',boldItalic='TimesNewRoman-BoldItalic')
styles=getSampleStyleSheet()
styles.add(ParagraphStyle(name='ReportTitle',fontName='TimesNewRoman-Bold',fontSize=18,leading=22,spaceAfter=12))
styles.add(ParagraphStyle(name='Deck',fontName='TimesNewRoman',fontSize=11,leading=14,textColor=colors.black,spaceAfter=12))
styles['BodyText'].fontName='TimesNewRoman'
styles['BodyText'].fontSize=11;styles['BodyText'].leading=14;styles['BodyText'].spaceAfter=8
styles['Heading1'].fontName='TimesNewRoman-Bold'
styles['Heading1'].fontSize=14;styles['Heading1'].leading=18;styles['Heading1'].spaceAfter=10
styles['Heading2'].fontSize=11;styles['Heading2'].leading=15
styles.add(ParagraphStyle(name='SmallNote',fontName='TimesNewRoman',fontSize=9,leading=11,spaceAfter=7))
styles.add(ParagraphStyle(name='Cell',fontName='TimesNewRoman',fontSize=10,leading=12))
styles.add(ParagraphStyle(name='CodeBlock',fontName='Courier',fontSize=8,leading=11,spaceAfter=8))
story=[]
def p(text,style='BodyText'): story.append(Paragraph(text,styles[style]))
def h(text):p(text,'Heading1')
def table(data,widths,small=False):
    cells=[[Paragraph(str(c),styles['Cell']) for c in row] for row in data]
    t=Table(cells,colWidths=widths,repeatRows=1,hAlign='LEFT')
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#eeeeee')),
        ('GRID',(0,0),(-1,-1),.4,colors.HexColor('#d9d9d9')),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),('TOPPADDING',(0,0),(-1,-1),5),
        ('BOTTOMPADDING',(0,0),(-1,-1),5),('LEFTPADDING',(0,0),(-1,-1),7),
        ('RIGHTPADDING',(0,0),(-1,-1),7)]))
    story.append(t);story.append(Spacer(1,10))
def picture(name,width=475):
    path=FIG/name; w,h=ImageReader(str(path)).getSize();story.append(Image(str(path),width=width,height=width*h/w))
def page():story.append(PageBreak())
def f(cond,s,field='median_ms'):return stats[cond,s][field]
ratio=f('flushed',256)/f('flushed',1)

p('Initial sequential scan experiment','ReportTitle')
p('Radix hash join project | Windows 11 measurements','Deck')
p(f'We read 200,000 bytes in every scan and changed how far apart those reads were. After flushing the '
  f'selected cache lines, the median scan time went from {f("flushed",1):.4f} ms at stride 1 to '
  f'{f("flushed",256):.4f} ms at stride 256, about {ratio:.0f} times as long.')

h('How we ran the scan')
p('We filled a 256 MiB array with repeatable random byte values. The stride is the gap between '
  'successive reads. At stride 8, for example, the program reads positions 0, 8, 16 and so on. '
  'The scan ran on one logical processor of an AMD Ryzen 7 7840HS with 64-byte cache lines. '
  'The core has 32 KiB of L1 data cache and 1 MiB of L2 cache. The CPU shares 16 MiB of L3 cache.')
p('<b>Flushed scan.</b> Before starting the timer, we removed the selected data lines from the '
  'CPU caches using CLFLUSH. The CPU could still fetch and reuse those lines during the scan.')
p('<b>Immediate repeat scan.</b> We read the same positions once before starting the timer, '
  'then timed a second scan. Some of the data from the first pass could still be in cache.')
p(f'We tested 11 strides from 1 to 1024 bytes, doubling the gap each time. We recorded '
  f'{meta["repetitions"]} runs per stride and method after three warmup rounds. Each round used '
  f'a shuffled order. We timed reading and adding the bytes, then checked the sum after the '
  f'timer stopped. All {len(rows)} scans passed that check.')
p('The test follows the basic setup in Section 2 of the paper [1]. This report uses the Windows '
  'measurements. The Ubuntu instructions in the README use their own set of timings.')

page()
h('What changed as the stride grew')
picture('01_primary_stride_scan.png')
p('Figure 1. Scan times after flushing the selected data. The shaded band runs from the 5th to '
  'the 95th percentile of the measured times at each stride.','SmallNote')
p(f'The time stayed close to 0.09 ms at small strides. The biggest jump was from '
  f'{f("flushed",64):.3f} ms at stride 64 to {f("flushed",128):.3f} ms at stride 128. '
  'With small gaps, several reads can share one cache line. At stride 64 and above, every read '
  'selects a separate line. The scan still slows down beyond 64, even though the number of '
  'selected lines stays the same.')
data=[['Stride (B)','Mean (ms)','Median (ms)','SD (ms)','P95 (ms)','Min (ms)','Max (ms)']]
for s in main:
    x=stats['flushed',s]
    data.append([s]+[f'{x[k]:.4f}' for k in ['mean_ms','median_ms','sample_sd_ms','p95_ms','min_ms','max_ms']])
table(data,[61,69,69,69,69,69,69])
p('SD measures how much the runs varied. P95 is the 95th percentile.','SmallNote')

page()
h('Did reading the data beforehand help?')
picture('02_cache_preparation.png',460)
p('Figure 2. Median scan times for both methods. Each step on the horizontal axis doubles the stride.','SmallNote')
p(f'At stride 64, the median fell from {f("flushed",64):.4f} ms to {f("repeat_scan",64):.4f} ms '
  'when we read the data once beforehand. The difference was small at 256 and nearly disappeared '
  'at 512. At 1024, the repeat scan was slightly quicker again, although both methods took longer '
  'than at 512.')
p('At stride 1024, the read positions stretch across about 195 MiB of addresses. They still select '
  'only 200,000 lines, or about 12.2 MiB of cache line data. The larger address span can affect '
  'prefetching and address translation. We measured scan time, so we cannot tell how much each '
  'factor contributed.')
p('[1] P. Boncz, S. Manegold and M. L. Kersten. Database Architecture Optimized for the New '
  'Bottleneck: Memory Access. Proceedings of VLDB, 1999. Sections 2 and 3.','SmallNote')
def footer(canvas,doc):
    canvas.setFont('TimesNewRoman',9);canvas.setFillColor(colors.black)
    canvas.drawCentredString(297.64,26,str(doc.page))
report=OUT/'initial_scan_report.pdf'
doc=SimpleDocTemplate(str(report),pagesize=(595.28,841.89),rightMargin=42,leftMargin=42,
                      topMargin=38,bottomMargin=42,title='Initial sequential scan experiment',
                      author='Radix hash join project')
doc.build(story,onFirstPage=footer,onLaterPages=footer)
# Remove automatically generated creation and modification timestamps from PDF metadata.
from pypdf import PdfReader, PdfWriter
reader=PdfReader(report)
writer=PdfWriter()
writer.append_pages_from_reader(reader)
writer.add_metadata({'/Title':'Initial sequential scan experiment','/Author':'Radix hash join project'})
with report.open('wb') as target:
    writer.write(target)
print(json.dumps({'report':str(report),'validation':validation,'stride256_to_stride1_median_ratio':ratio},indent=2))
