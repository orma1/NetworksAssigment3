#split file line by line into dictionary like so key:value
def readConfigFile(file):
    with open(file) as config: #open file with default closing
        config_dict = {} #create empty dictionary
        for line in config: #go over each line in the file
            line = line.strip() #should make the line empty in case of whitespace
            if line: #if line is not empty
                try:
                    key, value = line.split(':') #split line by semicolon into key:value
                    config_dict[key] = value #set the value of key
                except ValueError:
                    print("invalid file format") #if no semicolon, the format is not ok
        return config_dict #after finishing creating the dictionary send it back

