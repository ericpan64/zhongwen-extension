import spacy
from spacy.tokenizer import Tokenizer
import socket
from socket import error as SocketError
import errno
import selectors
import types
import time
import pandas as pd
from pypinyin import pinyin as pfmt
from pypinyin import Style
import opencc
from config import TOKENIZER_HOST, TOKENIZER_PORT, MAX_BUF, SORTED_CEDICT_CSV_PATH
import sys

# NLP import from: https://spacy.io/models/zh
nlp = spacy.load("zh_core_web_sm")
tokenizer = nlp.Defaults.create_tokenizer(nlp)

# Traditional -> Simplified Converter (~67% less lossy than Simp->Trad for CEDICT)
trad_converter = opencc.OpenCC('t2s.json')

# Socket code adapted from: https://realpython.com/python-sockets
sel = selectors.DefaultSelector()
IPV4 = socket.AF_INET
TCP = socket.SOCK_STREAM

CEDICT_DF = pd.read_csv(SORTED_CEDICT_CSV_PATH, index_col=0)
CEDICT_SET = set(CEDICT_DF.iloc[:, 0]).union(set(CEDICT_DF.iloc[:, 1]))

# Chinese chars use 3 bytes, so a phrase with Chinese chars will return false.
# Ex. len('京报') = 2
#     len(bytes('京报', 'utf-8')) = 6
entire_phrase_is_english = lambda p: len(p) == len(bytes(p, 'utf-8'))
entire_phrase_is_chinese = lambda p: (3 * len(p)) == len(bytes(p, 'utf-8'))
flatten_list = lambda l: [i for j in l for i in j] # [[a], [b], [c]] => [a, b, c]

def break_down_large_token_into_subtoken_list(t):
    """
    Applies divide & conquer approach to break-down a larger token into list of CEDICT-only components
    """
    # Base cases: found in CEDICT, or Chinese punctuation, or English phrase
    if t in CEDICT_SET or len(t) == 1 or entire_phrase_is_english(t):
        return [t]
    n = len(t)
    mid = int(n/2) # round down for odd #'s
    left_tokens = break_down_large_token_into_subtoken_list(t[0:mid])
    right_tokens = break_down_large_token_into_subtoken_list(t[mid: n])
    return left_tokens + right_tokens

def tokenize_str(s):
    """
    Given the input text s, tokenize and return as $-delimited phrases,
        where each phrase is `-delimited in the format: simp_phrase`raw_pinyin`formatted_pinyin
        Pinyin is space-delimited for a multi-word phrase
    Example:
        Input : "祝你有美好的天！"
        Output: "祝`zhu4$你`ni3$有`you3$美好`mei3 hao3$的`de5$天`tian1$！`!"
    """
    # Convert to Simplified, then tokenize
    s = trad_converter.convert(s)
    tokens = tokenizer(s)
    n_pinyin  = len(s)
    # str_tokens = [str(t) for t in tokens]
    # Break-down phrases that are not in CEDICT to lesser components
    str_tokens = [''] * max([len(t) for t in tokens]) * len(tokens) # pre-allocate upper-bound, clean-up after
    j = 0
    for i in range(len(tokens)):
        t = str(tokens[i])
        if t == ' ':
            continue
        elif t in CEDICT_SET or not entire_phrase_is_chinese(t):
            str_tokens[j] = t
            j += 1
        else:
            # use divide-and-conquer approach: recursively split until all tokens are accounted for
            subtokens = break_down_large_token_into_subtoken_list(t)
            n_st = len(subtokens)
            str_tokens[j: n_st] = subtokens
            j += n_st
    while str_tokens[-1] == '':
        str_tokens.pop()
    # Handle special characters to match tokenizer output
    # for special characters within an alphanumeric phrase, tokenizer splits it but pfmt doesn't
    # for spaces, tokenizer ignores but pfmt doesn't
    def get_corrected_syntax_for_pinyin_list(style):
        init_pinyin_list = flatten_list(pfmt(s, style=style, neutral_tone_with_five=True))
        pinyin_list = [''] * n_pinyin # pre-allocate since known size
        i, j = 0, 0
        while i < len(init_pinyin_list) and j < n_pinyin:
            curr_pinyin = init_pinyin_list[i]
            curr_pinyin = [str(t) for t in list(tokenizer(curr_pinyin))]
            phrase_len = len(curr_pinyin)
            pinyin_list[j:j+phrase_len] = curr_pinyin
            j += phrase_len
            i += 1
        pinyin_list = [py for py in pinyin_list if py not in {'', ' '}]
        return pinyin_list
    reversed_raw_pinyin_list = get_corrected_syntax_for_pinyin_list(Style.TONE3)[::-1] # works
    # Generate delimited string
    n_tokens = len(str_tokens)
    delimited_list = [''] * n_tokens # pre-allocate since known size
    for i in range(n_tokens):
        if entire_phrase_is_english(str_tokens[i]):
            raw_pinyin = reversed_raw_pinyin_list.pop()
        else:
            raw_pyin_list = [''] * len(str_tokens[i])
            for j in range(len(str_tokens[i])):
                raw_pyin_list[j] = reversed_raw_pinyin_list.pop()
            raw_pinyin = ' '.join(raw_pyin_list)
        delimited_list[i] = f"{str_tokens[i]}`{raw_pinyin}"
    delimited_str = '$'.join(delimited_list)
    return delimited_str

def accept_wrapper(sock):
    conn, addr = sock.accept()
    print('accepted connection from: ', addr)
    conn.setblocking(False)
    data = types.SimpleNamespace(addr=addr, inb=b'', outb=b'')
    events = selectors.EVENT_READ | selectors.EVENT_WRITE
    sel.register(conn, events, data=data)

def service_connection(key, mask):
    sock = key.fileobj
    data = key.data
    if mask & selectors.EVENT_READ:
        try:
            recv_data = sock.recv(MAX_BUF)  # Should be ready to read
            if recv_data:
                # run NLP parser, then send results back
                recv_data = tokenize_str(str(recv_data ,'utf-8'))
                data.outb += bytes(recv_data, 'utf-8')
            else:
                print('closing connection to', data.addr)
                sel.unregister(sock)
                sock.close()
        except SocketError as e:
            if e.errno != errno.ECONNRESET:
                raise e
            print(f'connection to {data.addr} was reset by sender')
            pass
    if mask & selectors.EVENT_WRITE:
        if data.outb:
            # print('sending', repr(data.outb), 'to', data.addr)
            sent = sock.send(data.outb)  # Should be ready to write
            print(f"number of bytes sent from tokenizer: {sent}", file=sys.stderr)
            time.sleep(1.0)
            data.outb = data.outb[sent:]

if __name__ == '__main__':
    # Multi-threaded connections
    print("Starting socket server...")
    lsock  = socket.socket(IPV4, TCP)
    lsock.bind((TOKENIZER_HOST, TOKENIZER_PORT))
    lsock.listen()
    print('listening on', (TOKENIZER_HOST, TOKENIZER_PORT))
    lsock.setblocking(False)
    sel.register(lsock, selectors.EVENT_READ, data=None)

    while True:
        events = sel.select(timeout=None)
        for key, mask in events:
            if key.data is None:
                accept_wrapper(key.fileobj)
            else:
                service_connection(key, mask)

    # # Single threaded connections
    # with socket.socket(IPV4, TCP) as s:
    #     s.bind((TOKENIZER_HOST, TOKENIZER_PORT))
    #     s.listen()
    #     conn, addr = s.accept()
    #     with conn:
    #         print('Connected by: ', addr)
    #         while True:
    #             data = conn.recv(MAX_BUF)
    #             if not data:
    #                 break
    #             data = tokenize_str(str(data, 'utf-8'))
    #             conn.sendall(bytes(data, 'utf-8'))